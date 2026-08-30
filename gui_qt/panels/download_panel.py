"""download_panel — 视频下载面板（阶段2 迁移自 gui/panels/download_panel.py + main.py 下载逻辑）。

URL 队列式下载（yt-dlp，core.video_downloader）：解析格式、添加/批量导入链接、
Cookie/代理/限速/仅音频等设置，任务经 TaskManager 通用链路串行执行。
"""
import os
import re
import logging
from functools import partial
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer, Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtWidgets import (QBoxLayout, QFileDialog, QHBoxLayout,
                               QWidget, QVBoxLayout)
from qfluentwidgets import (FluentIcon, CaptionLabel, CheckBox, ComboBox,
                            LineEdit, ListWidget, PrimaryPushButton, PushButton,
                            TextEdit)

from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt import task_manager as tm
from gui_qt.widgets import ActionBar


logger = logging.getLogger(__name__)

# 预置值（与 tkinter 版 download_panel 一致）
SPEED_VALUES = [tr("不限", "Unlimited"), "2", "5", "10", "20", "50"]
AUDIO_FMT_VALUES = ["mp3", "m4a", "flac", "wav", "opus"]
MAX_QUEUE_ITEMS = 1000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_INPUT_CHARS = 256 * 1024
AUTO_FORMAT_HINT = (
    "可直接加入队列，默认自动选格式；仅需指定画质时解析单条链接。",
    "Add links directly for automatic format selection. Parse a single link only to choose a specific format.",
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ORPHAN_PARSE_WORKERS = set()


def _release_orphan_worker(worker):
    _ORPHAN_PARSE_WORKERS.discard(worker)
    worker.deleteLater()


def _clean_url(raw):
    """提取首个具备主机名的 HTTP(S) URL；普通文本不再误判为链接。"""
    raw = str(raw or "").strip()
    m = re.search(r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef<>\"']+", raw)
    if not m:
        return ""
    value = m.group(0).rstrip(".,;:!?)，。；：！？）】}")
    if len(value) > 4096:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        parsed.port
    except (ValueError, UnicodeError):
        return ""
    return value


def _extract_urls(text, limit=MAX_QUEUE_ITEMS):
    """按出现顺序提取并去重，限制队列规模。"""
    result = []
    seen = set()
    for line in str(text or "").splitlines():
        url = _clean_url(line)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
            if len(result) >= limit:
                break
    return result


def _safe_download_name(value, fallback="video"):
    """远程标题只能生成当前保存目录内的普通文件名。"""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(value or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")[:120]
    if not name or name.upper().split(".", 1)[0] in _WINDOWS_RESERVED:
        name = fallback
    return name


def _validate_template(value):
    template = str(value or "").strip()
    if not template:
        return None
    if (template in {".", ".."} or os.path.isabs(template)
            or "/" in template or "\\" in template
            or re.search(r'[<>:"|?*]', template)
            or any(ord(char) < 32 for char in template)
            or template.endswith((".", " "))):
        raise ValueError("文件名模板不能包含路径、父目录或控制字符")
    return template


def _normalize_proxy(value):
    proxy = str(value or "").strip()
    if not proxy:
        return None
    if any(char in proxy for char in "\r\n\t "):
        raise ValueError("代理地址不能包含空白或换行")
    candidate = proxy if "://" in proxy else f"http://{proxy}"
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
            raise ValueError
        if not parsed.hostname:
            raise ValueError
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError("代理格式无效，例如 http://127.0.0.1:7890") from None
    return candidate


class _ParseWorker(SafeWorker):
    """后台解析视频格式（yt-dlp 联网，可能耗时）。"""

    sig_done = Signal(list, str, object)   # (formats, title, playlist)

    def __init__(self, url, cookie, proxy, headers, parent=None):
        super().__init__(parent)
        self.url = url
        self._cookie, self._proxy, self._headers = cookie, proxy, headers

    def work(self):
        from core.video_downloader import VideoDownloader
        dl = VideoDownloader()
        fmts, title, _thumb, playlist = dl.get_formats(
            self.url, cookie=self._cookie, proxy=self._proxy,
            headers=self._headers)
        self.sig_done.emit(fmts or [], title or "", playlist)



class DownloadPanelPage(BaseQtPanel):
    """视频下载页。"""

    panel_key = "download"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频下载", "Video download"),
            tr("支持 B站 / YouTube / 微博 / Instagram 等数百个平台",
               "Supports Bilibili / YouTube / Weibo / Instagram and hundreds of sites"),
            FluentIcon.DOWNLOAD))

        # URL 输入区
        from gui_qt.components.form_widgets import FormSection, FormGrid
        card = FormSection(tr("链接与格式", "Link & format"), FluentIcon.EDIT)
        url_body = QWidget()
        vl = QVBoxLayout(url_body)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)
        self.txt_url = TextEdit()
        self.txt_url.setFixedHeight(64)
        self.txt_url.setPlaceholderText(
            tr("粘贴视频链接，每行一个，支持批量…",
               "Paste video links, one per line…"))
        self.txt_url.setAccessibleName(tr("视频链接", "Video links"))
        self.txt_url.setAcceptRichText(False)
        from gui_qt.components import design_system as _ds
        _ds.apply_text_edit_style(self.txt_url)
        self._url_cleaning = False
        self.txt_url.textChanged.connect(self._on_url_changed)
        vl.addWidget(self.txt_url)
        self.url_actions = QBoxLayout(QBoxLayout.LeftToRight)
        self.url_actions.setSpacing(8)
        primary_wrap = QWidget(self)
        primary_row = QHBoxLayout(primary_wrap)
        primary_row.setContentsMargins(0, 0, 0, 0)
        primary_row.setSpacing(8)
        self.btn_parse = PushButton(
            FluentIcon.SEARCH, tr("解析格式", "Parse formats"))
        self.btn_parse.clicked.connect(self._parse_url)
        self.btn_add = PrimaryPushButton(
            FluentIcon.ADD, tr("加入队列", "Add to queue"))
        self.btn_add.clicked.connect(self._add_url)
        self.btn_import = PushButton(
            FluentIcon.FOLDER, tr("批量导入", "Import batch"))
        self.btn_import.clicked.connect(self._batch_import)
        for button in (self.btn_parse, self.btn_add, self.btn_import):
            primary_row.addWidget(button)
        primary_row.addStretch(1)
        secondary_wrap = QWidget(self)
        secondary_row = QHBoxLayout(secondary_wrap)
        secondary_row.setContentsMargins(0, 0, 0, 0)
        secondary_row.setSpacing(8)
        self.btn_fav = PushButton(FluentIcon.HEART, tr("收藏", "Favorite"))
        self.btn_fav.clicked.connect(self._add_favorite)
        self.btn_favs = PushButton(FluentIcon.LIBRARY, tr("收藏夹", "Favorites"))
        self.btn_favs.clicked.connect(self._show_favorites)
        self.btn_hist = PushButton(FluentIcon.HISTORY, tr("历史", "History"))
        self.btn_hist.clicked.connect(self._show_history)
        for button in (self.btn_hist, self.btn_favs, self.btn_fav):
            secondary_row.addWidget(button)
        secondary_row.addStretch(1)
        self.url_actions.addWidget(primary_wrap)
        self.url_actions.addStretch(1)
        self.url_actions.addWidget(secondary_wrap)
        vl.addLayout(self.url_actions)
        self.format_row = QWidget()
        row = QHBoxLayout(self.format_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(CaptionLabel(tr("选择格式", "Format")))
        self.lst_formats = ListWidget()
        self.lst_formats.setAccessibleName(tr("可用下载格式", "Available formats"))
        self.lst_formats.setMinimumHeight(84)
        self.lst_formats.setMaximumHeight(128)
        row.addWidget(self.lst_formats, 1)
        vl.addWidget(self.format_row)
        self.format_row.setVisible(False)
        self.lb_fmt_info = CaptionLabel(tr(*AUTO_FORMAT_HINT))
        self.lb_fmt_info.setWordWrap(True)
        self.lb_fmt_info.setTextFormat(Qt.PlainText)
        vl.addWidget(self.lb_fmt_info)
        card.add_widget(url_body)
        lay.addWidget(card)

        # 设置区
        from gui_qt.components.form_widgets import CollapsibleSection
        set_card = FormSection(tr("下载设置", "Download settings"), FluentIcon.DOWNLOAD)
        self.lb_settings_summary = CaptionLabel()
        self.lb_settings_summary.setWordWrap(True)
        self.lb_settings_summary.setTextFormat(Qt.PlainText)
        set_card.add_widget(self.lb_settings_summary)
        self.advanced_section = CollapsibleSection(
            tr("高级设置", "Advanced"),
            hint=tr("Cookie / 代理 / 限速 / 请求头 / 命名 / 音频 / 字幕",
                    "Cookie / proxy / speed / headers / naming / audio / subtitles"))
        self.adv_grid = FormGrid(columns=3)
        self._responsive_edits = []

        self.ed_cookie = self.adv_grid.add_field(
            "Cookie", self._line_edit(200, ""),
            hint=tr("可粘贴 Cookie 字符串或选择已有 Cookie 文件路径",
                    "Paste a cookie string or enter a cookie file path"))
        self.ed_cookie.setMaxLength(16384)
        self.ed_cookie.setPlaceholderText(
            tr("Cookie 字符串或文件路径…", "Cookie string or file path…"))
        self.ed_proxy = self.adv_grid.add_field(
            tr("代理", "Proxy"), self._line_edit(150, ""),
            hint=tr("HTTP/HTTPS 代理地址", "HTTP/HTTPS proxy"))
        self.ed_proxy.setPlaceholderText("http://127.0.0.1:7890…")
        self.ed_proxy.setMaxLength(2048)
        # 设置里「网络代理」开启时预填为默认值（用户可手动覆盖）
        try:
            if self.services.get_pref("proxy_mode", "off") == "manual":
                _host = self.services.get_pref("proxy_host", "")
                _port = self.services.get_pref("proxy_port", 0)
                if _host and int(_port) > 0:
                    self.ed_proxy.setText(f"{_host}:{int(_port)}")
        except Exception:  # noqa: BLE001 - 默认值失败不影响页面构建
            logger.exception("读取默认代理设置失败")
        self.cb_speed = self.adv_grid.add_field(
            tr("限速 MB/s", "Speed limit MB/s"), self._combo(SPEED_VALUES, tr("不限", "Unlimited")),
            hint=tr("下载速度上限，不限为 0", "Download speed limit, 0 = unlimited"))
        self.ed_headers = self.adv_grid.add_field(
            tr("请求头", "Headers"),
            self._line_edit(0, "Referer: https://example.com, User-Agent: …"),
            hint=tr("自定义请求头，逗号分隔", "Custom headers, comma separated"))
        self.ed_headers.setMaxLength(8192)
        self.ed_template = self.adv_grid.add_field(
            tr("文件名模板", "Filename template"),
            self._line_edit(200, tr("例如：%(title)s.%(ext)s…",
                                    "Example: %(title)s.%(ext)s…")),
            hint=tr("输出文件名模板，留空使用默认", "Output name template, blank = default"))
        self.ed_template.setMaxLength(255)
        self.option_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.option_layout.setSpacing(8)
        audio_wrap = QWidget(self)
        audio_row = QHBoxLayout(audio_wrap)
        audio_row.setContentsMargins(0, 0, 0, 0)
        audio_row.setSpacing(8)
        self.cb_audio_only = CheckBox(tr("仅音频", "Audio only"))
        self.cb_audio_only.toggled.connect(self._toggle_audio)
        audio_row.addWidget(self.cb_audio_only)
        self.cb_audio_fmt = ComboBox()
        self.cb_audio_fmt.addItems(AUDIO_FMT_VALUES)
        self.cb_audio_fmt.setCurrentText("mp3")
        self.cb_audio_fmt.setEnabled(False)
        audio_row.addWidget(self.cb_audio_fmt)
        self.option_layout.addWidget(audio_wrap)
        self.cb_subtitles = CheckBox(tr("下载字幕", "Download subtitles"))
        self.option_layout.addWidget(self.cb_subtitles)
        self.cb_thumb = CheckBox(tr("保存封面", "Thumbnail"))
        self.option_layout.addWidget(self.cb_thumb)
        self.cb_video_only = CheckBox(tr("仅视频(无音频)", "Video only"))
        self.cb_video_only.toggled.connect(self._toggle_video)
        self.option_layout.addWidget(self.cb_video_only)
        self.option_layout.addStretch(1)
        r3_box = QWidget()
        r3_box.setLayout(self.option_layout)
        self.adv_grid.add_field(tr("下载内容", "Content"), r3_box, colspan=1)
        self.advanced_section.add_layout(self.adv_grid)
        set_card.add_widget(self.advanced_section)
        lay.addWidget(set_card)

        # 保存目录
        self.dir_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.dir_layout.setSpacing(8)
        dir_label = CaptionLabel(tr("保存到", "Save to"))
        self.dir_layout.addWidget(dir_label)
        self.ed_dir = LineEdit()
        self.ed_dir.setText(os.path.expanduser("~/Downloads"))
        self.ed_dir.setAccessibleName(tr("保存目录", "Save folder"))
        dir_label.setBuddy(self.ed_dir)
        self.dir_layout.addWidget(self.ed_dir, 1)
        self.btn_browse = PushButton(tr("浏览", "Browse"))
        self.btn_browse.clicked.connect(self._browse_dir)
        self.dir_layout.addWidget(self.btn_browse)
        self.btn_open_dir = PushButton(tr("打开输出文件夹", "Open output folder"))
        self.btn_open_dir.clicked.connect(self._open_output_folder)
        self.dir_layout.addWidget(self.btn_open_dir)
        lay.addLayout(self.dir_layout)

        # 下载队列
        q_card = FormSection(tr("下载队列", "Download queue"), FluentIcon.MENU)
        q_body = QWidget()
        ql = QVBoxLayout(q_body)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(8)
        qhead = QHBoxLayout()
        qhead.setSpacing(8)
        self.lb_count = CaptionLabel(tr("0 个任务", "0 tasks"))
        qhead.addStretch(1)
        qhead.addWidget(self.lb_count)
        ql.addLayout(qhead)
        self.lb_queue_empty = CaptionLabel(
            tr("添加链接后，任务会按顺序显示在这里",
               "Added links will appear here in download order"))
        ql.addWidget(self.lb_queue_empty)
        self.lst_queue = ListWidget()
        self.lst_queue.setAccessibleName(tr("下载队列", "Download queue"))
        self.lst_queue.setMinimumHeight(110)
        ql.addWidget(self.lst_queue)
        self.queue_controls = QWidget()
        qbtn = QHBoxLayout(self.queue_controls)
        qbtn.setContentsMargins(0, 0, 0, 0)
        qbtn.setSpacing(8)
        self.btn_up = PushButton(FluentIcon.UP, tr("上移", "Move up"))
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = PushButton(FluentIcon.DOWN, tr("下移", "Move down"))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_remove = PushButton(
            FluentIcon.REMOVE, tr("移除选中", "Remove selected"))
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = PushButton(
            FluentIcon.DELETE, tr("清空队列", "Clear queue"))
        self.btn_clear.clicked.connect(self._clear_queue)
        for b in (self.btn_up, self.btn_down, self.btn_remove, self.btn_clear):
            qbtn.addWidget(b)
        qbtn.addStretch(1)
        ql.addWidget(self.queue_controls)
        q_card.add_widget(q_body)
        lay.addWidget(q_card)

        self.action_bar = ActionBar(tr("开始下载", "Download"))
        lay.addWidget(self.action_bar)

        # 运行态
        self._queue = []            # [{"url","name","fmt_id"}]
        self._formats = []
        self._title = ""
        self._task_rows = {}        # task_id -> queue 行号
        self._batch_results = []
        self._worker = None
        self._parsed_url = None
        self._submitting = False
        self.lst_queue.currentRowChanged.connect(self._sync_queue_controls)
        for control in (self.cb_audio_only, self.cb_video_only,
                        self.cb_subtitles, self.cb_thumb):
            control.toggled.connect(self._refresh_download_summary)
        self.cb_audio_fmt.currentIndexChanged.connect(self._refresh_download_summary)
        self.cb_speed.currentIndexChanged.connect(self._refresh_download_summary)
        self._refresh_download_summary()
        self._update_count()
        mgr = self.services.task_manager
        mgr.sig_progress.connect(self._on_progress)
        mgr.sig_state.connect(self._on_state)
        self.action_bar.btn_go.clicked.connect(self._start)
        self.action_bar.btn_go.setEnabled(False)
        self.action_bar.btn_cancel.clicked.connect(self._cancel_all)

    # ── 表单辅助 ─────────────────────────────────
    def _combo(self, items, default):
        cb = ComboBox()
        cb.addItems(items)
        cb.setCurrentText(default)
        return cb

    def _line_edit(self, width, placeholder):
        ed = LineEdit()
        if width > 0:
            ed.setFixedWidth(width)
            if hasattr(self, "_responsive_edits"):
                self._responsive_edits.append((ed, width))
        if placeholder:
            ed.setPlaceholderText(placeholder)
        return ed

    # ── URL 操作 ─────────────────────────────────
    def _on_url_changed(self):
        """输入改变立即使旧解析结果失效，避免格式套用到另一链接。"""
        self._check_douyin_tip()
        current = _clean_url(self.txt_url.toPlainText()[:MAX_INPUT_CHARS])
        if self._parsed_url and current != self._parsed_url:
            self._parsed_url = None
            self._formats = []
            self._title = ""
            self.lst_formats.clear()
            self.format_row.setVisible(False)
            self.lb_fmt_info.setText(
                tr("链接已更改，可直接加入队列或重新解析格式。",
                   "Link changed. Add to the queue or parse formats again.")
                if current else tr(*AUTO_FORMAT_HINT))

    def _check_douyin_tip(self):
        """检测到抖音/TikTok 链接时显示 Cookie 必填提示（仅首次）；内容清空/切换后自动复位。"""
        if not hasattr(self, '_douyin_tip_shown'):
            self._douyin_tip_shown = False
        text = self.txt_url.toPlainText()[:MAX_INPUT_CHARS]
        has_douyin = any(d in text.lower() for d in ("douyin.com", "tiktok.com", "v.douyin.com", "vt.tiktok.com"))
        # 无抖音链接时复位，允许下一次输入再次触发
        if not has_douyin:
            self._douyin_tip_shown = False
            return
        if self._douyin_tip_shown:
            return
        self._douyin_tip_shown = True
        from gui_qt.components import toast
        toast.show_warning(
            self,
            tr("检测到抖音/TikTok 链接\n", "Douyin/TikTok link detected\n")
 +
            tr("平台要求有效 Cookie 才能解析\n", "The platform requires a valid cookie to parse\n")
 +
            tr("请在上方「Cookie」框粘贴浏览器完整 Cookie，\n", "Paste your full browser cookie in the \"Cookie\" field above,\n")
 +
            tr("或命令行启动：python main_qt.py --cookies-from-browser chrome", "or launch: python main_qt.py --cookies-from-browser chrome"),
            duration=8000
        )

    def _parse_url(self):
        raw = self.txt_url.toPlainText()
        if len(raw) > MAX_INPUT_CHARS:
            toast.show_warning(
                self, tr("链接输入内容过长，请使用批量导入文件",
                         "Link input is too large; use batch import"))
            return
        urls = _extract_urls(raw, limit=2)
        if not urls:
            toast.show_warning(self, tr("未检测到有效URL", "No valid URL detected"))
            return
        if len(urls) > 1:
            # 解析只针对一个视频，不能悄悄删除用户粘贴的其他待下载链接。
            toast.show_info(self, tr("解析格式一次仅支持一条链接。批量链接可直接加入队列。",
                                     "Parse one link at a time. Add multiple links directly to the queue."))
            return
        if self._worker is not None and self._worker.isRunning():
            toast.show_info(self, tr("正在解析，请稍候", "Parsing in progress…"))
            return
        url = urls[0]
        try:
            proxy = _normalize_proxy(self.ed_proxy.text())
            headers = self._parse_headers()
        except ValueError as exc:
            toast.show_warning(self, str(exc))
            return
        self.txt_url.setPlainText(url)
        self._parsed_url = None
        self.lst_formats.clear()
        self.format_row.setVisible(False)
        self.lb_fmt_info.setText(tr("正在获取格式信息…", "Fetching format info…"))
        self.btn_parse.setEnabled(False)
        self._worker = _ParseWorker(url, self.ed_cookie.text().strip() or None,
                                    proxy, headers, self)
        self._worker.sig_done.connect(
            lambda fmts, title, playlist, source=url:
            self._on_formats(fmts, title, playlist, source))
        self._worker.sig_error.connect(
            lambda error, source=url: self._on_parse_fail(error, source))
        self._worker.finished.connect(self._on_parse_finished)
        self._worker.start()

    def _on_formats(self, fmts, title, playlist, source_url=None):
        if source_url and _clean_url(self.txt_url.toPlainText()) != source_url:
            self.lb_fmt_info.setText(
                tr("链接已更改，已忽略旧解析结果",
                   "Link changed; outdated result ignored."))
            return
        self._formats = fmts
        self._title = title
        self._parsed_url = source_url or _clean_url(self.txt_url.toPlainText())
        self.lb_fmt_info.setToolTip(title or "")
        self.lst_formats.clear()
        for f in fmts:
            sz = f"{f['filesize'] / 1024 / 1024:.0f}MB" if f.get("filesize") else "?"
            self.lst_formats.addItem(
                f"[{f.get('format_id')}] {f.get('ext')}  " +
                f"{f.get('resolution', '')}  {sz}")
        info = tr("已识别：{}", "Recognized: {}").format((title or '')[:60])
        if playlist:
            info += (tr("  |  播放列表: {} ", "  |  Playlist: {} ").format(playlist.get('title', '')) +
                     tr("({}个视频)", "({} videos)").format(playlist.get('count', 0)))
        self.lb_fmt_info.setText(info)
        self.format_row.setVisible(bool(fmts))
        if not fmts:
            self.lb_fmt_info.setText(tr("未找到可用格式", "No format available"))
        self._sync_start_enabled()

    def _on_parse_fail(self, err, source_url=None):
        if source_url and _clean_url(self.txt_url.toPlainText()) != source_url:
            return
        self._parsed_url = None
        self._formats = []
        self.format_row.setVisible(False)
        detail = str(err or tr("未知错误", "Unknown error")).strip()
        short = detail[:120] + ("…" if len(detail) > 120 else "")
        self.lb_fmt_info.setText(
            tr("获取失败：{}", "Fetch failed: {}").format(short))
        self.lb_fmt_info.setToolTip(detail)
        toast.show_error(
            self, tr("视频链接解析失败：{}",
                     "Could not parse video link: {}").format(detail[:300]))
        self._sync_start_enabled()

    def _on_parse_finished(self):
        self.btn_parse.setEnabled(True)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _add_url(self):
        raw = self.txt_url.toPlainText()
        if len(raw) > MAX_INPUT_CHARS:
            toast.show_warning(
                self, tr("链接输入内容过长，请使用批量导入文件",
                         "Link input is too large; use batch import"))
            return
        urls = _extract_urls(raw)
        if not urls:
            toast.show_warning(self, tr("请输入有效URL", "Enter a valid URL"))
            return
        existing = {item["url"] for item in self._queue}
        capacity = MAX_QUEUE_ITEMS - len(self._queue)
        if capacity <= 0:
            toast.show_warning(
                self, tr("队列已达到 1000 个任务上限",
                         "The queue limit of 1000 tasks has been reached"))
            return
        added = 0
        for url in urls[:capacity]:
            if url in existing:
                continue
            parsed = url == self._parsed_url
            host = urlsplit(url).hostname or "video"
            name = _safe_download_name(
                self._title if parsed else host, fallback="video")
            fmt_id = None
            row = self.lst_formats.currentRow() if parsed else -1
            if parsed and 0 <= row < len(self._formats):
                fmt_id = self._formats[row].get("format_id")
            format_label = fmt_id or tr("自动格式", "Automatic")
            display = f"  {name[:30]}  [{format_label}]  -  {url[:50]}"
            self._queue.append({"url": url, "name": name,
                                "fmt_id": fmt_id, "display": display})
            self.lst_queue.addItem(display)
            existing.add(url)
            added += 1
        self._update_count()
        self.txt_url.clear()
        self._douyin_tip_shown = False  # 重置提示，允许新链接再次触发
        if added:
            toast.show_success(self, tr("已添加 {} 个链接到队列", "Added {} links to queue").format(added))
        else:
            toast.show_info(self, tr("链接已在下载队列中", "Links already in queue"))

    def _batch_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择链接文件", "Pick link file"), "", tr("文本文件 (*.txt);;所有文件 (*.*)", "Text files (*.txt);;All files (*.*)"))
        if not path:
            return
        try:
            if os.path.getsize(path) > MAX_IMPORT_BYTES:
                toast.show_warning(
                    self, tr("链接文件超过 2 MB 限制",
                             "The link file exceeds the 2 MB limit"))
                return
            with open(path, "rb") as file:
                payload = file.read(MAX_IMPORT_BYTES + 1)
            if len(payload) > MAX_IMPORT_BYTES:
                raise ValueError(tr("链接文件超过 2 MB 限制",
                                    "The link file exceeds the 2 MB limit"))
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = payload.decode("gb18030")
        except (OSError, UnicodeError, ValueError) as exc:
            toast.show_error(
                self, tr("无法读取链接文件：{}",
                         "Cannot read link file: {}").format(exc))
            return
        existing = {item["url"] for item in self._queue}
        capacity = MAX_QUEUE_ITEMS - len(self._queue)
        if capacity <= 0:
            toast.show_warning(
                self, tr("队列已达到 1000 个任务上限",
                         "The queue limit of 1000 tasks has been reached"))
            return
        added = 0
        for url in _extract_urls(text, limit=capacity):
            if url in existing:
                continue
            display = f"  [{tr('自动格式', 'Automatic')}]  {url[:60]}"
            name = _safe_download_name(urlsplit(url).hostname, "video")
            self._queue.append({"url": url, "name": name,
                                "fmt_id": None, "display": display})
            self.lst_queue.addItem(display)
            existing.add(url)
            added += 1
        self._update_count()
        if added:
            toast.show_success(self, tr("成功导入 {} 个链接", "Imported {} links").format(added))
        else:
            toast.show_warning(self, tr("未找到有效链接", "No valid links found"))

    def _add_favorite(self):
        url = _clean_url(self.txt_url.toPlainText())
        if not url:
            toast.show_warning(self, tr("请先输入链接", "Enter links first"))
            return
        from core.m3u8_downloader import M3U8Store
        M3U8Store().add_favorite(url, name=self._title or url[:40], note="")
        toast.show_success(self, tr("已收藏", "Saved"))

    def _show_favorites(self):
        from core.m3u8_downloader import M3U8Store
        from gui_qt.panels.url_list_dialog import UrlListDialog
        store = M3U8Store()

        def use(url, name):
            cleaned = _clean_url(url)
            if (cleaned and len(self._queue) < MAX_QUEUE_ITEMS
                    and not any(q["url"] == cleaned for q in self._queue)):
                safe_name = _safe_download_name(name, "video")
                display = f"  {safe_name[:30]}  [{tr('自动格式', 'Automatic')}]  -  {cleaned[:50]}"
                self._queue.append({"url": cleaned, "name": safe_name,
                                    "fmt_id": None, "display": display})
                self.lst_queue.addItem(display)
                self._update_count()

        dlg = UrlListDialog(tr("收藏链接", "Saved links"), store.get_favorites(),
                            use, self, kind="favorites",
                            delete_fn=store.remove_favorite,
                            clear_fn=store.clear_favorites)
        dlg.exec()

    def _show_history(self):
        from core.m3u8_downloader import M3U8Store
        from gui_qt.panels.url_list_dialog import UrlListDialog
        store = M3U8Store()

        def use(url, name):
            cleaned = _clean_url(url)
            if cleaned:
                self._title = name
                self.txt_url.setPlainText(cleaned)

        dlg = UrlListDialog(tr("下载历史", "Download history"), store.get_history(),
                            use, self, kind="history",
                            delete_fn=store.remove_history,
                            clear_fn=store.clear_history)
        dlg.exec()

    # ── 队列操作 ─────────────────────────────────
    def _update_count(self):
        has_tasks = bool(self._queue)
        self.lb_count.setText(tr("{} 个任务", "{} tasks").format(len(self._queue)))
        self.lb_queue_empty.setVisible(not has_tasks)
        self.lst_queue.setVisible(has_tasks)
        self.queue_controls.setVisible(has_tasks)
        # 行内保持紧凑，悬停仍能核对完整地址和该链接保存的格式选择。
        for row, entry in enumerate(self._queue):
            item = self.lst_queue.item(row)
            if item is not None:
                item.setToolTip(tr("链接：{}\n格式：{}", "URL: {}\nFormat: {}").format(
                    entry["url"], entry.get("fmt_id") or tr("自动选择", "Automatic")))
        self._sync_queue_controls()
        self._sync_start_enabled()

    def _sync_queue_controls(self, _row=-1):
        if not hasattr(self, "_queue"):
            return
        row = self.lst_queue.currentRow()
        idle = not self._task_rows
        selected = 0 <= row < len(self._queue)
        self.btn_up.setEnabled(idle and selected and row > 0)
        self.btn_down.setEnabled(idle and selected and row < len(self._queue) - 1)
        self.btn_remove.setEnabled(idle and selected)
        self.btn_clear.setEnabled(idle and bool(self._queue))

    def _sync_start_enabled(self):
        if hasattr(self, "action_bar"):
            self.action_bar.btn_go.setEnabled(
                bool(self._queue) and not bool(self._task_rows)
                and not self._submitting)

    def _move(self, delta):
        if self._task_rows:
            return
        row = self.lst_queue.currentRow()
        j = row + delta
        if row < 0 or j < 0 or j >= len(self._queue):
            return
        self._queue[row], self._queue[j] = self._queue[j], self._queue[row]
        item = self.lst_queue.takeItem(row)
        self.lst_queue.insertItem(j, item)
        self.lst_queue.setCurrentRow(j)

    def _remove_selected(self):
        if self._task_rows:
            return
        for row in sorted(
                {i.row() for i in self.lst_queue.selectedIndexes()},
                reverse=True):
            self.lst_queue.takeItem(row)
            if row < len(self._queue):
                self._queue.pop(row)
        self._update_count()

    def _clear_queue(self):
        if not self._queue or self._task_rows:
            return
        from qfluentwidgets import MessageBox
        try:
            box = MessageBox(
                tr("清空下载队列", "Clear download queue"),
                tr("确定移除队列中的全部链接吗？",
                   "Remove all links from the queue?"), self)
            box.yesButton.setText(tr("清空", "Clear"))
            box.cancelButton.setText(tr("取消", "Cancel"))
            confirmed = bool(box.exec())
        except Exception as exc:  # noqa: BLE001 - 无法确认时保持队列不变
            toast.show_error(
                self, tr("无法显示清空确认框：{}",
                         "Could not confirm clearing: {}").format(exc))
            return
        if not confirmed:
            return
        self.lst_queue.clear()
        self._queue.clear()
        self._update_count()
        self.action_bar.btn_go.setEnabled(False)
        self._douyin_tip_shown = False  # 重置提示

    # ── 设置辅助 ─────────────────────────────────
    def _refresh_download_summary(self, *_):
        if self.cb_audio_only.isChecked():
            mode = tr("仅音频 ({})", "Audio only ({})").format(self.cb_audio_fmt.currentText().upper())
        elif self.cb_video_only.isChecked():
            mode = tr("仅视频，无声音", "Video only, no audio")
        else:
            mode = tr("常规下载，格式以队列为准", "Standard download, per-link format")
        extras = []
        if self.cb_subtitles.isChecked():
            extras.append(tr("字幕", "subtitles"))
        if self.cb_thumb.isChecked():
            extras.append(tr("封面", "thumbnail"))
        extra_text = tr("、", ", ").join(extras) if extras else tr("无附加文件", "no extras")
        speed = self.cb_speed.currentText()
        limit = (tr("不限速", "unlimited speed") if self.cb_speed.currentIndex() == 0
                 else f"{speed} MB/s")
        self.lb_settings_summary.setText(f"{mode} / {extra_text} / {limit}")

    def _toggle_audio(self, checked):
        self.cb_audio_fmt.setEnabled(checked)
        if checked and self.cb_video_only.isChecked():
            self.cb_video_only.setChecked(False)

    def _toggle_video(self, checked):
        if checked and self.cb_audio_only.isChecked():
            self.cb_audio_only.setChecked(False)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, tr("选择下载目录", "Pick download folder"),
                                             self.ed_dir.text() or "")
        if d:
            self.ed_dir.setText(d)

    def _open_output_folder(self):
        d = self.ed_dir.text().strip()
        if d and os.path.isdir(d):
            from utils.platform_utils import open_path
            if not open_path(d):
                toast.show_error(self, tr("无法打开输出目录", "Cannot open output folder"))
        else:
            toast.show_warning(self, tr("输出目录不存在", "Output folder does not exist"))

    # ── 提交下载 ─────────────────────────────────
    def _start(self):
        if self._task_rows or self._submitting:
            return
        # add_task 可能触发界面事件，在首个任务号返回前也要拦住重复提交。
        self._submitting = True
        self._sync_start_enabled()
        try:
            self._submit_downloads()
        finally:
            self._submitting = False
            self._sync_start_enabled()

    def _submit_downloads(self):
        if not self._queue:
            toast.show_warning(self, tr("请先添加下载链接", "Add download links first"))
            return
        out_dir = self.ed_dir.text().strip()
        if not out_dir:
            toast.show_warning(self, tr("请选择保存目录", "Choose a save folder"))
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            toast.show_error(self, tr("无法创建输出目录：{}", "Cannot create output folder: {}").format(e))
            return
        out_dir = os.path.abspath(out_dir)
        self.ed_dir.setText(out_dir)
        try:
            proxy = _normalize_proxy(self.ed_proxy.text())
            headers = self._parse_headers()
            output_template = _validate_template(self.ed_template.text())
            if (output_template and len(self._queue) > 1
                    and not re.search(
                        r"%\((?:id|title|display_id|autonumber|playlist_index)\)",
                        output_template)):
                raise ValueError(
                    tr("批量下载的文件名模板必须包含 title、id 或自动编号字段",
                       "Batch templates must include title, id, or an auto-number field"))
        except ValueError as exc:
            toast.show_warning(self, str(exc))
            return
        self.save_prefs()

        from core.video_downloader import VideoDownloader

        speed_str = self.cb_speed.currentText()
        params = {
            "cookie": self.ed_cookie.text().strip() or None,
            "proxy": proxy,
            "speed_limit": 0 if speed_str == tr("不限", "Unlimited") else int(speed_str),
            "audio_only": self.cb_audio_only.isChecked(),
            "audio_format": self.cb_audio_fmt.currentText(),
            "subtitles": self.cb_subtitles.isChecked(),
            "thumbnail": self.cb_thumb.isChecked(),
            "video_only": self.cb_video_only.isChecked(),
            "output_template": output_template,
            "headers": headers,
            "out_dir": out_dir,
        }
        mgr = self.services.task_manager
        if not self._task_rows:
            self._batch_results = []
        added = 0
        reserved = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task in mgr.all_tasks()
            if task.state in (tm.WAITING, tm.RUNNING, tm.PAUSED)
            and task.output_path
        }
        for i, item in enumerate(self._queue):
            url = item["url"]
            name = _safe_download_name(item["name"], "video")
            ext = params["audio_format"] if params["audio_only"] else "mp4"
            output_path = os.path.join(out_dir, f"{name}.{ext}")
            output_key = os.path.normcase(os.path.abspath(output_path))
            if os.path.exists(output_path) or output_key in reserved:
                base, ext2 = os.path.splitext(output_path)
                c = 1
                while (os.path.exists(f"{base}_{c}{ext2}")
                       or os.path.normcase(os.path.abspath(
                           f"{base}_{c}{ext2}")) in reserved):
                    c += 1
                output_path = f"{base}_{c}{ext2}"
            reserved.add(os.path.normcase(os.path.abspath(output_path)))
            p = dict(params)
            p["url"] = url
            p["format_id"] = item.get("fmt_id")
            downloader = VideoDownloader()
            tid = mgr.add_task(
                name=f"{tr('下载', 'Download')} - {name}", task_type="download",
                file_path="", output_path=output_path, params=p,
                runner=partial(self._runner, downloader),
                canceller=downloader.cancel,
                history_type=tr("视频下载", "Video Download"), history_target=tr("视频下载", "Video Download"),
                need_ffmpeg=False,
                sensitive_param_keys=("cookie", "headers", "proxy"),
                allow_auto_recover=False)
            if tid is not None:
                self._task_rows[tid] = i
                added += 1
        if added:
            self.action_bar.set_running(True)
            self.queue_controls.setEnabled(False)
            self.lst_queue.setEnabled(False)
            self.action_bar.set_status(tr("已提交 {} 个下载任务", "Submitted {} download tasks").format(added))
        else:
            toast.show_error(self, tr("任务提交失败", "Submit failed"))

    @staticmethod
    def _parse_headers_from(text):
        headers = {}
        if text:
            for pair in text.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if ":" not in pair:
                    raise ValueError(f"请求头缺少冒号：{pair}")
                key, value = pair.split(":", 1)
                key, value = key.strip(), value.strip()
                if (not _HEADER_NAME_RE.fullmatch(key)
                        or any(char in value for char in "\r\n")):
                    raise ValueError(f"无效请求头：{key or '?'}")
                headers[key] = value
        return headers

    def _parse_headers(self):
        return self._parse_headers_from(self.ed_headers.text().strip())

    def _runner(self, downloader, task, prog):
        p = task.params
        return downloader.download(
            p.get("url", ""), task.output_path,
            format_id=p.get("format_id"), progress_callback=prog,
            cookie=p.get("cookie"), headers=p.get("headers"),
            proxy=p.get("proxy"), speed_limit=p.get("speed_limit", 0),
            audio_only=p.get("audio_only", False),
            audio_format=p.get("audio_format", "mp3"),
            subtitles=p.get("subtitles", False),
            output_template=p.get("output_template"),
            thumbnail=p.get("thumbnail", False),
            video_only=p.get("video_only", False))

    def _cancel_all(self):
        mgr = self.services.task_manager
        for tid in list(self._task_rows):
            mgr.cancel_task(tid)
        self.action_bar.btn_cancel.setEnabled(False)

    # ── 进度/状态联动 ────────────────────────────
    def _on_progress(self, task_id, pct, msg, speed):
        if task_id not in self._task_rows:
            return
        self.action_bar.set_status(msg)
        if pct >= 0:
            self.action_bar.set_total(pct)

    def _on_state(self, task_id, state):
        if task_id not in self._task_rows:
            return
        row = self._task_rows[task_id]
        if (0 <= row < self.lst_queue.count()
                and row < len(self._queue)):
            self.lst_queue.item(row).setText(
                f"{self._queue[row]['display']}  [{tm.state_text(state)}]")
        if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
            self._batch_results.append(state)
            self._task_rows.pop(task_id, None)
        active = [self.services.task_manager.get_task(t)
                  for t in self._task_rows]
        if not any(t and t.state in (tm.WAITING, tm.RUNNING, tm.PAUSED)
                   for t in active):
            self.action_bar.set_batch_result(
                self._batch_results.count(tm.SUCCESS),
                self._batch_results.count(tm.FAILED),
                self._batch_results.count(tm.CANCELLED))
            self.queue_controls.setEnabled(True)
            self.lst_queue.setEnabled(True)
            self._sync_queue_controls()
            self._sync_start_enabled()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 820
        self.url_actions.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.option_layout.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.dir_layout.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.adv_grid.set_columns(1 if narrow else 3)
        for edit, wide_width in self._responsive_edits:
            if narrow:
                edit.setMinimumWidth(0)
                edit.setMaximumWidth(16777215)
            else:
                edit.setFixedWidth(wide_width)
        self._refresh_responsive_layout()
        QTimer.singleShot(0, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        """方向变化后立即清除旧 sizeHint，避免窄屏沿用宽屏最小宽高。"""
        for layout in (self.url_actions, self.option_layout, self.dir_layout,
                       self.adv_grid):
            layout.invalidate()
            layout.activate()
        self.content_layout.invalidate()
        self.content_layout.activate()
        self.content.setMinimumWidth(0)
        self.content.updateGeometry()

    def closeEvent(self, event):
        """解析线程可能仍在联网；关闭页面时断开回调，避免访问已销毁控件。"""
        worker = self._worker
        self._worker = None
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(500):
                for signal in (worker.sig_done, worker.sig_error,
                               worker.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                worker.setParent(None)
                _ORPHAN_PARSE_WORKERS.add(worker)
                worker.finished.connect(
                    lambda current=worker: _release_orphan_worker(current))
        super().closeEvent(event)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "url": self.txt_url.toPlainText().strip(),
            "cookie": self.ed_cookie.text(),
            "proxy": self.ed_proxy.text(),
            "speed": self.cb_speed.currentText(),
            "headers": self.ed_headers.text(),
            "audio_only": self.cb_audio_only.isChecked(),
            "audio_fmt": self.cb_audio_fmt.currentText(),
            "subtitles": self.cb_subtitles.isChecked(),
            "thumbnail": self.cb_thumb.isChecked(),
            "video_only": self.cb_video_only.isChecked(),
            "template": self.ed_template.text(),
            "dir": self.ed_dir.text(),
        }

    def collect_prefs(self) -> dict:
        speed = self.cb_speed.currentText()
        return {
            "dl_dir": self.ed_dir.text(),
            "speed_limit": 0 if speed == tr("不限", "Unlimited") else int(speed),
            "audio_only": self.cb_audio_only.isChecked(),
            "audio_format": self.cb_audio_fmt.currentText(),
            "subtitles": self.cb_subtitles.isChecked(),
            "thumbnail": self.cb_thumb.isChecked(),
            "video_only": self.cb_video_only.isChecked(),
            "output_template": self.ed_template.text(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("dl_dir"):
            self.ed_dir.setText(str(prefs["dl_dir"]))
        speed = str(prefs.get("speed_limit", 0))
        self.cb_speed.setCurrentText(
            tr("不限", "Unlimited") if speed == "0" else speed)
        audio_format = str(prefs.get("audio_format", "mp3"))
        if audio_format in AUDIO_FMT_VALUES:
            self.cb_audio_fmt.setCurrentText(audio_format)
        self.cb_subtitles.setChecked(bool(prefs.get("subtitles", False)))
        self.cb_thumb.setChecked(bool(prefs.get("thumbnail", False)))
        self.cb_video_only.setChecked(bool(prefs.get("video_only", False)))
        self.cb_audio_only.setChecked(bool(prefs.get("audio_only", False)))
        try:
            template = _validate_template(prefs.get("output_template", ""))
        except ValueError:
            template = None
        self.ed_template.setText(template or "")
