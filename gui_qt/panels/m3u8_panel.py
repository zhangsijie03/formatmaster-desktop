"""m3u8_panel — M3U8 视频下载面板（阶段2 迁移自 gui/panels/m3u8_panel.py + main.py 下载逻辑）。

URL 队列式 M3U8 下载（core.m3u8_downloader）：解析画质/字幕、批量添加、
多线程并发下载、断点续传、字幕下载，任务经 TaskManager 通用链路串行执行。
"""
import hashlib
import os
import re
from functools import partial
from urllib.parse import unquote, urlparse, urlsplit

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtWidgets import (QBoxLayout, QFileDialog, QHBoxLayout,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (FluentIcon, CaptionLabel, CheckBox, ComboBox,
                            LineEdit, ListWidget, PrimaryPushButton, PushButton,
                            TextEdit)

from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormSection, FormGrid
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt import task_manager as tm
from gui_qt.widgets import ActionBar

# 预置值（与 tkinter 版 m3u8_panel 一致）
THREADS_VALUES = ["4", "8", "16", "24", "32", "48", "64"]
FORMAT_VALUES = ["mp4", "mkv", "avi", "mov", "ts"]
SPEED_VALUES = [tr("不限", "Unlimited"), "2", "5", "10", "20", "50"]
MAX_QUEUE_ITEMS = 1000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_INPUT_CHARS = 256 * 1024
AUTO_QUALITY = tr("自动选择最高画质", "Automatic best quality")
QUALITY_HINT = tr("可直接加入队列；单个链接加入后自动解析，可再选择画质。",
                  "Add directly to queue. A single link is parsed automatically for quality selection.")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ORPHAN_QUALITY_WORKERS = set()


def _release_orphan_worker(worker):
    _ORPHAN_QUALITY_WORKERS.discard(worker)
    worker.deleteLater()

# 队列行状态视觉（对标 Fluent-M3U8 任务卡的状态圆点 + 状态色）
_QUEUE_STYLE = {
    "waiting": ("#5F6472", "●"),     # 待下载：灰点
    "running": ("#2F6BFF", "▶"),     # 运行中：蓝点
    "success": ("#0FA47A", "✓"),     # 成功：绿勾
    "failed":  ("#E5484D", "✕"),     # 失败：红叉
    "cancelled": ("#9AA0AC", "○"),   # 已取消：空心灰
}


def _dot_icon(color):
    """12px 彩色圆点图标（队列行状态指示，与任务卡风格一致）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 10, 10)
    p.end()
    return QIcon(pm)


def _clean_m3u8_url(raw):
    """提取首个具备主机名的 HTTP(S) URL，拒绝畸形和超长输入。"""
    match = re.search(
        r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef<>\"']+",
        str(raw or "").strip())
    if not match:
        return ""
    value = match.group(0).rstrip(".,;:!?)，。；：！？）】}")
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


def _extract_m3u8_urls(text, limit=MAX_QUEUE_ITEMS):
    """按输入顺序提取并去重，防止集合打乱队列顺序。"""
    result = []
    seen = set()
    for line in str(text or "").splitlines():
        url = _clean_m3u8_url(line)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
            if len(result) >= limit:
                break
    return result


def _safe_download_name(value, fallback="video"):
    """远程链接和用户输入只能生成当前输出目录内的普通文件名。"""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(value or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")[:120]
    if not name or name.upper().split(".", 1)[0] in _WINDOWS_RESERVED:
        return fallback
    return name


def _safe_subtitle_lang(value):
    """字幕语言标识只参与文件名后缀，过滤路径与控制字符。"""
    lang = re.sub(r"[^0-9A-Za-z_-]", "_", str(value or "und"))[:20]
    return lang or "und"


def _normalize_proxy(value):
    proxy = str(value or "").strip()
    if not proxy:
        return None
    if any(char in proxy for char in "\r\n\t "):
        raise ValueError(tr("代理地址不能包含空白或换行",
                            "Proxy cannot contain whitespace or line breaks"))
    candidate = proxy if "://" in proxy else f"http://{proxy}"
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
            raise ValueError
        if not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError(tr("代理格式无效，例如 http://127.0.0.1:7890",
                            "Invalid proxy, for example http://127.0.0.1:7890")) from None
    return candidate


def _parse_headers(text):
    """解析界面请求头，并阻止换行注入及无效字段名。"""
    headers = {}
    if text:
        for pair in text.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if ":" not in pair:
                raise ValueError(tr("请求头缺少冒号：{}", "Header is missing a colon: {}").format(pair))
            key, value = pair.split(":", 1)
            key, value = key.strip(), value.strip()
            if (not _HEADER_NAME_RE.fullmatch(key)
                    or any(char in value for char in "\r\n")):
                raise ValueError(tr("无效请求头：{}", "Invalid header: {}").format(key or "?"))
            headers[key] = value
    return headers


class _QualityWorker(SafeWorker):
    """后台解析 M3U8 画质与字幕轨道（联网请求）。"""

    sig_done = Signal(list, list, str)   # (qualities, subs, source_url)
    sig_fail = Signal(str, str)

    def __init__(self, dl, url, headers, cookie, proxy, parent=None):
        super().__init__(parent)
        self._dl, self._url = dl, url
        self._headers, self._cookie, self._proxy = headers, cookie, proxy

    def work(self):
        try:
            qualities = self._dl.get_qualities(
                self._url, headers=self._headers,
                cookie=self._cookie, proxy=self._proxy)
            subs = self._dl.get_subtitles(
                self._url, headers=self._headers,
                cookie=self._cookie, proxy=self._proxy)
            self.sig_done.emit(qualities or [], subs or [], self._url)
        except Exception as e:  # noqa: BLE001
            self.sig_fail.emit(str(e), self._url)



class M3u8PanelPage(BaseQtPanel):
    """M3U8 下载页。"""

    panel_key = "m3u8"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self._responsive_edits = []
        # 使用标准标题结构，确保右侧任务操作组对齐内容区右边缘。旧版把
        # PageHeader 塞进横向布局，标题只取得内容宽度，执行按钮因此紧贴标题。
        lay.addWidget(self.make_title(tr("M3U8 视频下载", "M3U8 download")))
        lay.addWidget(CaptionLabel(
            tr("添加多个链接，支持画质选择，批量队列下载",
               "Add multiple links, pick quality, batch queue download")))

        # URL 输入区
        card = FormSection(tr("M3U8 链接", "M3U8 link"), FluentIcon.DOWNLOAD)
        body = QWidget()
        vl = QVBoxLayout(body)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)
        self.txt_url = TextEdit()
        self.txt_url.setFixedHeight(64)
        self.txt_url.setPlaceholderText(tr("每行一个 M3U8 链接，支持批量粘贴…", "One M3U8 link per line, batch supported…"))
        self.txt_url.setAccessibleName(tr("M3U8 链接", "M3U8 links"))
        self.txt_url.setAcceptRichText(False)
        self.txt_url.textChanged.connect(self._on_url_changed)
        from gui_qt.components import design_system as _ds
        _ds.apply_text_edit_style(self.txt_url)
        vl.addWidget(self.txt_url)
        self.url_actions = QBoxLayout(QBoxLayout.LeftToRight)
        brow = self.url_actions
        brow.setSpacing(8)
        btn_add = PrimaryPushButton(tr("加入队列", "Add to queue"))
        btn_add.clicked.connect(self._batch_add)
        btn_fav = PushButton(FluentIcon.HEART, tr("收藏", "Favorite"))
        btn_fav.clicked.connect(self._add_favorite)
        self.btn_parse = PushButton(tr("解析画质", "Parse quality"))
        self.btn_parse.clicked.connect(self._parse_url)
        brow.addWidget(btn_add)
        brow.addWidget(btn_fav)
        brow.addWidget(self.btn_parse)
        brow.addStretch(1)
        vl.addLayout(brow)
        self.quality_layout = QBoxLayout(QBoxLayout.LeftToRight)
        qrow = self.quality_layout
        qrow.setSpacing(8)
        qrow.addWidget(CaptionLabel(tr("画质", "Quality")))
        self.cb_quality = ComboBox()
        self.cb_quality.addItem(AUTO_QUALITY)
        self.cb_quality.setEnabled(False)
        self.cb_quality.currentIndexChanged.connect(self._quality_changed)
        qrow.addWidget(self.cb_quality, 1)
        vl.addLayout(qrow)
        self.lb_quality_hint = CaptionLabel(QUALITY_HINT)
        self.lb_quality_hint.setWordWrap(True)
        self.lb_quality_hint.setTextFormat(Qt.PlainText)
        vl.addWidget(self.lb_quality_hint)
        card.add_widget(body)
        lay.addWidget(card)

        # 文件名 + 保存目录
        self.path_layout = QBoxLayout(QBoxLayout.LeftToRight)
        nrow = self.path_layout
        nrow.setSpacing(8)
        nrow.addWidget(CaptionLabel(tr("文件名", "File name")))
        self.ed_name = LineEdit()
        self.ed_name.setPlaceholderText(tr("留空则自动命名…", "Leave blank to name automatically…"))
        self.ed_name.setAccessibleName(tr("文件名", "File name"))
        nrow.addWidget(self.ed_name, 1)
        nrow.addWidget(CaptionLabel(tr("保存到", "Save to")))
        self.ed_dir = LineEdit()
        self.ed_dir.setText(os.path.expanduser("~/Downloads"))
        self.ed_dir.setAccessibleName(tr("保存目录", "Save folder"))
        nrow.addWidget(self.ed_dir, 1)
        btn_browse = PushButton(tr("浏览", "Browse"))
        btn_browse.clicked.connect(self._browse_dir)
        nrow.addWidget(btn_browse)
        lay.addLayout(nrow)

        # 下载设置
        from gui_qt.components.form_widgets import CollapsibleSection
        set_card = FormSection(tr("下载设置", "Download settings"), FluentIcon.SETTING)
        self.settings_grid = FormGrid(columns=2)
        grid = self.settings_grid

        self.cb_threads = grid.add_field(
            tr("并发线程", "Concurrent threads"), self._combo(THREADS_VALUES, "16"),
            hint=tr("并发下载的线程数", "Concurrent download threads"))
        self.cb_format = grid.add_field(
            tr("输出格式", "Output format"), self._combo(FORMAT_VALUES, "mp4"),
            hint=tr("输出视频容器格式", "Output video container"))
        set_card.add_form(grid)
        self.lb_settings_summary = CaptionLabel()
        self.lb_settings_summary.setWordWrap(True)
        self.lb_settings_summary.setTextFormat(Qt.PlainText)
        set_card.add_widget(self.lb_settings_summary)

        self.advanced_section = CollapsibleSection(
            tr("高级设置", "Advanced"),
            hint=tr("限速 / Cookie / 代理 / 请求头 / 断点续传 / 字幕 / 通知",
                    "Speed / cookie / proxy / headers / resume / subtitles / notify"))
        adv = self.advanced_section
        self.adv_grid = FormGrid(columns=2)
        adv_grid = self.adv_grid
        self.cb_speed = adv_grid.add_field(
            tr("限速 MB/s", "Speed limit MB/s"), self._combo(SPEED_VALUES, tr("不限", "Unlimited")),
            hint=tr("下载速度上限，不限为 0", "Download speed limit, 0 = unlimited"))
        self.ed_cookie = adv_grid.add_field(
            "Cookie", self._line_edit(180, ""),
            hint=tr("登录 Cookie，用于访问受限资源", "Login cookie for restricted content"))
        self.ed_cookie.setEchoMode(LineEdit.Password)
        self.ed_proxy = adv_grid.add_field(
            tr("代理", "Proxy"),
            self._line_edit(180, tr("如 http://127.0.0.1:7890", "e.g. http://127.0.0.1:7890")),
            hint=tr("HTTP/HTTPS 代理地址", "HTTP/HTTPS proxy"))
        self.ed_headers = adv_grid.add_field(
            tr("自定义Header", "Custom headers"), self._line_edit(0, "Key:Value,Key:Value"),
            hint=tr("自定义请求头，逗号分隔", "Custom headers, comma separated"))
        adv.add_layout(adv_grid)
        sec_grid_box = QWidget()
        self.option_layout = QBoxLayout(QBoxLayout.LeftToRight, sec_grid_box)
        chk_lay = self.option_layout
        chk_lay.setContentsMargins(0, 0, 0, 0)
        self.cb_resume = CheckBox(tr("断点续传", "Resume download"))
        self.cb_resume.setChecked(True)
        self.cb_download_sub = CheckBox(tr("同时下载字幕", "Also download subtitles"))
        self.cb_notify = CheckBox(tr("完成通知", "Notify when done"))
        self.cb_notify.setChecked(True)
        chk_lay.addWidget(self.cb_resume)
        chk_lay.addWidget(self.cb_download_sub)
        chk_lay.addWidget(self.cb_notify)
        chk_lay.addStretch(1)
        adv.add_widget(sec_grid_box)
        set_card.add_widget(adv)
        lay.addWidget(set_card)

        # 下载队列
        q_card = FormSection(tr("下载队列", "Download queue"), FluentIcon.MENU)
        qhead = QHBoxLayout()
        qhead.setSpacing(8)
        qhead.addStretch(1)
        self.lb_count = CaptionLabel(tr("0 个任务", "0 tasks"))
        qhead.addWidget(self.lb_count)
        q_body = QWidget()
        ql = QVBoxLayout(q_body)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(8)
        ql.addLayout(qhead)
        self.lb_queue_hint = CaptionLabel(tr("队列为空，请先在上方添加链接。", "Queue empty. Add links above to begin."))
        self.lb_queue_hint.setWordWrap(True)
        ql.addWidget(self.lb_queue_hint)
        self.lst_queue = ListWidget()
        self.lst_queue.setMinimumHeight(120)
        ql.addWidget(self.lst_queue)
        self.queue_controls = QWidget()
        qbtn = QBoxLayout(QBoxLayout.LeftToRight, self.queue_controls)
        qbtn.setContentsMargins(0, 0, 0, 0)
        qbtn.setSpacing(8)
        self.btn_up = b_up = PushButton(FluentIcon.UP, tr("上移", "Move up"))
        b_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = b_down = PushButton(FluentIcon.DOWN, tr("下移", "Move down"))
        b_down.clicked.connect(lambda: self._move(1))
        self.btn_remove = b_del = PushButton(FluentIcon.REMOVE, tr("移除选中", "Remove selected"))
        b_del.clicked.connect(self._remove_selected)
        self.btn_clear = b_clear = PushButton(tr("清空队列", "Clear queue"))
        b_clear.clicked.connect(self._clear_queue)
        b_import = PushButton(FluentIcon.FOLDER, tr("批量导入", "Import batch"))
        b_import.clicked.connect(self._batch_import)
        b_favs = PushButton(FluentIcon.HEART, tr("收藏链接", "Saved links"))
        b_favs.clicked.connect(self._show_favorites)
        b_hist = PushButton(FluentIcon.HISTORY, tr("历史记录", "History"))
        b_hist.clicked.connect(self._show_history)
        # 编辑与导入入口分组，窄屏换成两行，避免七个按钮撑宽内容区。
        for buttons in ((b_up, b_down, b_del, b_clear), (b_import, b_favs, b_hist)):
            group = QWidget()
            group_layout = QHBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(8)
            for button in buttons:
                group_layout.addWidget(button)
            group_layout.addStretch(1)
            qbtn.addWidget(group)
        qbtn.addStretch(1)
        ql.addWidget(self.queue_controls)
        q_card.add_widget(q_body)
        lay.addWidget(q_card)

        # 底部操作栏（ActionBar + 打开输出文件夹）
        bar_row = QHBoxLayout()
        bar_row.setSpacing(8)
        self.action_bar = ActionBar(tr("开始下载", "Download"))
        bar_row.addWidget(self.action_bar, 1)
        btn_open = PushButton(FluentIcon.FOLDER, tr("打开输出文件夹", "Open output folder"))
        btn_open.clicked.connect(self._open_output_folder)
        bar_row.addWidget(btn_open)
        lay.addLayout(bar_row)

        # 运行态
        from core.m3u8_downloader import M3U8Downloader
        self._m3u8_dl = M3U8Downloader()
        self._queue = []            # [{"url","master_url","name"}]
        self._qualities = []
        self._quality_source_url = None
        self._task_rows = {}        # task_id -> queue 行号
        self._batch_results = []
        self._worker = None
        self._submitting = False
        self.lst_queue.currentRowChanged.connect(self._sync_queue_controls)
        for control in (self.cb_resume, self.cb_download_sub):
            control.toggled.connect(self._refresh_download_summary)
        self.cb_speed.currentIndexChanged.connect(self._refresh_download_summary)
        self._refresh_download_summary()
        self._update_count()
        mgr = self.services.task_manager
        mgr.sig_progress.connect(self._on_progress)
        mgr.sig_state.connect(self._on_state)
        self.action_bar.btn_go.clicked.connect(self._start)
        self.action_bar.btn_cancel.clicked.connect(self._cancel_all)

    def _on_url_changed(self):
        """链接变化后让旧画质失效，避免将一个源的子清单套给另一个源。"""
        urls = _extract_m3u8_urls(self.txt_url.toPlainText()[:MAX_INPUT_CHARS], limit=2)
        if self._quality_source_url and urls != [self._quality_source_url]:
            self._quality_source_url = None
            self._qualities = []
            self.cb_quality.clear()
            self.cb_quality.addItem(AUTO_QUALITY)
            self.cb_quality.setEnabled(False)
            self.lb_quality_hint.setText(QUALITY_HINT)
            self.lb_quality_hint.setToolTip("")

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
            self._responsive_edits.append((ed, width))
        if placeholder:
            ed.setPlaceholderText(placeholder)
        return ed

    # ── 画质解析 ─────────────────────────────────
    def _parse_url(self):
        urls = _extract_m3u8_urls(self.txt_url.toPlainText()[:MAX_INPUT_CHARS], limit=2)
        if not urls:
            toast.show_warning(self, tr("请先输入M3U8链接", "Enter M3U8 links first"))
            return
        if len(urls) > 1:
            toast.show_info(self, tr("选择画质时请只保留一个链接；批量链接可直接加入队列。",
                                    "Keep one link to select quality; batch links can be added directly."))
            return
        self._parse_url_for(urls[0])

    def _parse_url_for(self, url):
        """解析指定 M3U8 的画质/字幕（后台线程），结果填入画质下拉。"""
        if self._worker is not None and self._worker.isRunning():
            toast.show_info(self, tr("正在解析，请稍候…", "Parsing; please wait…"))
            return
        try:
            headers = _parse_headers(self.ed_headers.text().strip())
            proxy = _normalize_proxy(self.ed_proxy.text())
        except ValueError as exc:
            toast.show_warning(self, str(exc))
            return
        self.btn_parse.setEnabled(False)
        self.cb_quality.setEnabled(False)
        self.lb_quality_hint.setToolTip("")
        self.lb_quality_hint.setText(tr("正在解析画质…", "Parsing quality…"))
        self._worker = _QualityWorker(
            self._m3u8_dl, url,
            headers,
            self.ed_cookie.text().strip() or None,
            proxy, self)
        self._worker.sig_done.connect(self._on_qualities)
        self._worker.sig_fail.connect(self._on_parse_fail)
        self._worker.finished.connect(self._on_parse_finished)
        self._worker.start()

    def _on_qualities(self, qualities, subs, source_url=None):
        # 网络回调可能晚于用户编辑，旧结果不能覆盖新链接的选择。
        if source_url and _extract_m3u8_urls(self.txt_url.toPlainText(), limit=2) != [source_url]:
            return
        self._quality_source_url = source_url
        self._qualities = qualities
        self.cb_quality.blockSignals(True)
        self.cb_quality.clear()
        if not qualities:
            self.cb_quality.addItem(tr("仅有一个画质", "Single quality"))
            hint = tr("该链接没有多码率选项，将使用默认画质", "No quality options for this link, using default")
        else:
            self.cb_quality.addItems([q["display"] for q in qualities])
            hint = tr("找到 {} 个画质，最高: {}", "Found {} qualities, best: {}").format(len(qualities), qualities[0]['label'])
        if subs:
            names = ", ".join(s["name"] for s in subs)
            hint += tr("  |  字幕: {}个 ({})", "  |  Subtitles: {} ({})").format(len(subs), names)
        else:
            hint += tr("  |  字幕: 无", "  |  Subtitles: none")
        self.cb_quality.blockSignals(False)
        self.cb_quality.setEnabled(bool(qualities) and not self._task_rows)
        self.lb_quality_hint.setToolTip("")
        self.lb_quality_hint.setText(hint)
        self._apply_quality_to_queue(self.cb_quality.currentIndex())

    def _on_parse_fail(self, err, _source_url=None):
        if _source_url and _extract_m3u8_urls(self.txt_url.toPlainText(), limit=2) != [_source_url]:
            return
        self._quality_source_url = None
        self._qualities = []
        self.cb_quality.clear()
        self.cb_quality.addItem(AUTO_QUALITY)
        self.cb_quality.setEnabled(False)
        detail = str(err or tr("未知错误", "Unknown error")).strip()
        short = detail[:100] + ("…" if len(detail) > 100 else "")
        self.lb_quality_hint.setText(
            tr("解析失败：{}，请检查链接或网络设置",
               "Parse failed: {}. Check the link or network settings.").format(short))
        self.lb_quality_hint.setToolTip(detail)
        toast.show_error(
            self, tr("无法解析 M3U8：{}",
                     "Could not parse M3U8: {}").format(detail[:300]))

    def _on_parse_finished(self):
        self.btn_parse.setEnabled(True)
        self.cb_quality.setEnabled(bool(self._qualities) and not self._task_rows)
        if not self._quality_source_url and not self.lb_quality_hint.toolTip():
            self.lb_quality_hint.setText(QUALITY_HINT)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _quality_changed(self, idx):
        if 0 <= idx < len(self._qualities):
            q = self._qualities[idx]
            hint = tr("已选: {}", "Selected: {}").format(q['label'])
            if q.get("resolution"):
                hint += f"  {q['resolution']}"
            if q.get("bandwidth_str"):
                hint += f"  {q['bandwidth_str']}"
            self.lb_quality_hint.setText(hint)
            self._apply_quality_to_queue(idx)

    def _apply_quality_to_queue(self, idx):
        """自动解析后选择画质，也同步到尚未提交的同源队列项。"""
        if self._task_rows or self._submitting or not (0 <= idx < len(self._qualities)):
            return
        quality = self._qualities[idx]
        for row, entry in enumerate(self._queue):
            if entry["master_url"] == self._quality_source_url:
                entry["url"] = quality["url"]
                entry["quality_label"] = quality["label"]
                entry["display"] = f"  {entry['name']}  [{quality['label']}]  —  {entry['master_url'][:50]}"
                self.lst_queue.item(row).setText(entry["display"])
        self._update_count()

    # ── 队列操作 ─────────────────────────────────
    def _add_queue_row(self, display):
        """创建队列行：待下载灰点 + 灰字（统一入口，状态更新走 _set_row_state）。"""
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QListWidgetItem
        item = QListWidgetItem(display)
        color = _QUEUE_STYLE["waiting"][0]
        item.setIcon(_dot_icon(color))
        item.setForeground(QColor("#8A9099"))
        self.lst_queue.addItem(item)
        return item

    def _set_row_state(self, row, state_key, extra=""):
        """按状态更新队列行：图标圆点 + 前景色 + 可选附加文本。"""
        from PySide6.QtGui import QColor
        if not (0 <= row < self.lst_queue.count()):
            return
        color, symbol = _QUEUE_STYLE.get(state_key, _QUEUE_STYLE["waiting"])
        item = self.lst_queue.item(row)
        if state_key in ("running", "success", "failed", "cancelled"):
            item.setIcon(_dot_icon(color))
            fg = {"success": "#0FA47A", "failed": "#E5484D",
                  "cancelled": "#9AA0AC"}.get(state_key, "#2F6BFF")
            item.setForeground(QColor(fg))
            item.setText(f"{self._queue[row]['display']}  {symbol} {extra}".rstrip())
        else:
            item.setIcon(_dot_icon(color))
            item.setForeground(QColor("#8A9099"))

    def _gen_name(self, url):
        name = self.ed_name.text().strip()
        if name:
            return _safe_download_name(name)
        base = unquote(os.path.basename(urlparse(url).path.rstrip("/")))
        if "." in base:
            base = os.path.splitext(base)[0]
        if base and 2 <= len(base) <= 30:
            return _safe_download_name(base)
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _batch_add(self):
        raw = self.txt_url.toPlainText()
        if len(raw) > MAX_INPUT_CHARS:
            toast.show_warning(self, tr("链接输入内容过长，请使用批量导入文件",
                                        "Link input is too large; use batch import"))
            return
        capacity = MAX_QUEUE_ITEMS - len(self._queue)
        if capacity <= 0:
            toast.show_warning(self, tr("队列已达到 1000 个任务上限", "The queue limit of 1000 tasks has been reached"))
            return
        urls = _extract_m3u8_urls(raw, limit=max(0, capacity))
        if not urls:
            toast.show_warning(self, tr("请先输入有效的M3U8链接", "Enter valid M3U8 links first"))
            return
        added = 0
        sel = self.cb_quality.currentIndex()
        existing = {item["master_url"] for item in self._queue}
        for url in urls:
            if url in existing:
                continue
            name = self._gen_name(url)
            quality_url = url
            quality_label = AUTO_QUALITY
            if (len(urls) == 1 and url == self._quality_source_url
                    and self._qualities and 0 <= sel < len(self._qualities)):
                quality_url = self._qualities[sel]["url"]
                quality_label = self._qualities[sel]['label']
            display = f"  {name}  [{quality_label}]  —  {url[:50]}"
            self._queue.append({"url": quality_url, "master_url": url,
                                "name": name, "display": display,
                                "quality_label": quality_label})
            self._add_queue_row(display)
            existing.add(url)
            added += 1
        self._update_count()
        if added:
            # 单链接保留在编辑区，自动解析完成后可直接调整它的队列画质。
            if len(urls) > 1:
                self.txt_url.clear()
            self.ed_name.clear()
            # 添加链接后自动解析画质（用户要求：无需再手动点「解析画质」）
            if len(urls) == 1 and urls[0] != self._quality_source_url:
                self._parse_url_for(urls[0])
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
                raise ValueError(tr("链接文件超过 2 MB 限制",
                                    "The link file exceeds the 2 MB limit"))
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
        capacity = MAX_QUEUE_ITEMS - len(self._queue)
        if capacity <= 0:
            toast.show_warning(self, tr("队列已达到 1000 个任务上限",
                                        "The queue limit of 1000 tasks has been reached"))
            return
        existing = {item["master_url"] for item in self._queue}
        added = 0
        for url in _extract_m3u8_urls(text, limit=capacity):
            if url in existing:
                continue
            base = unquote(os.path.basename(urlparse(url).path.rstrip("/")))
            if "." in base:
                base = os.path.splitext(base)[0]
            name = (_safe_download_name(base) if base and 2 <= len(base) <= 30
                    else hashlib.md5(url.encode()).hexdigest()[:12])
            display = f"  {name}  —  {url[:50]}"
            self._queue.append({"url": url, "master_url": url,
                                "name": name, "display": display})
            self._add_queue_row(display)
            existing.add(url)
            added += 1
        self._update_count()
        if added:
            toast.show_success(self, tr("成功导入 {} 个链接", "Imported {} links").format(added))
        else:
            toast.show_warning(self, tr("未找到有效链接", "No valid links found"))

    def _add_favorite(self):
        urls = _extract_m3u8_urls(self.txt_url.toPlainText(), limit=1)
        if not urls:
            toast.show_warning(self, tr("请先输入链接", "Enter links first"))
            return
        url = urls[0]
        path_parts = urlparse(url).path
        name = unquote(path_parts.split("/")[-1].split("?")[0])
        if not name or name.endswith(".m3u8"):
            name = _safe_download_name(self.ed_name.text().strip(), "video")
        self._m3u8_dl.store.add_favorite(url, name, "")
        toast.show_success(self, tr("已收藏: {}", "Saved: {}").format(name))

    def _show_favorites(self):
        from gui_qt.panels.url_list_dialog import UrlListDialog
        store = self._m3u8_dl.store

        def use(url, name):
            cleaned = _clean_m3u8_url(url)
            if (cleaned and len(self._queue) < MAX_QUEUE_ITEMS
                    and not any(q["master_url"] == cleaned for q in self._queue)):
                safe_name = _safe_download_name(name, "video")
                display = f"  {safe_name[:30]}  —  {cleaned[:50]}"
                self._queue.append({"url": cleaned, "master_url": cleaned,
                                    "name": safe_name,
                                    "display": display})
                self._add_queue_row(display)
                self._update_count()

        dlg = UrlListDialog(tr("收藏链接", "Saved links"), store.get_favorites(),
                            use, self, kind="favorites",
                            delete_fn=store.remove_favorite,
                            clear_fn=store.clear_favorites)
        dlg.exec()

    def _show_history(self):
        from gui_qt.panels.url_list_dialog import UrlListDialog
        store = self._m3u8_dl.store

        def use(url, name):
            if url:
                self.txt_url.setPlainText(url)
                self.ed_name.setText(name or "")

        dlg = UrlListDialog(tr("下载历史", "Download history"), store.get_history(),
                            use, self, kind="history",
                            delete_fn=store.remove_history,
                            clear_fn=store.clear_history)
        dlg.exec()

    def _update_count(self):
        self.lb_count.setText(tr("{} 个任务", "{} tasks").format(len(self._queue)))
        self.lb_queue_hint.setVisible(not self._queue)
        for row, entry in enumerate(self._queue):
            item = self.lst_queue.item(row)
            if item is not None:
                item.setToolTip(tr("源链接：{}\n画质：{}", "Source: {}\nQuality: {}").format(
                    entry["master_url"], entry.get("quality_label", AUTO_QUALITY)))
        self._sync_queue_controls()

    def _sync_queue_controls(self, _row=-1):
        if not hasattr(self, "_queue"):
            return
        row = self.lst_queue.currentRow()
        idle = not self._task_rows and not self._submitting
        selected = 0 <= row < len(self._queue)
        self.btn_up.setEnabled(idle and selected and row > 0)
        self.btn_down.setEnabled(idle and selected and row < len(self._queue) - 1)
        self.btn_remove.setEnabled(idle and selected)
        self.btn_clear.setEnabled(idle and bool(self._queue))
        self.action_bar.btn_go.setEnabled(idle and bool(self._queue))

    def _refresh_download_summary(self, *_):
        speed = (tr("不限速", "unlimited speed") if self.cb_speed.currentIndex() == 0
                 else f"{self.cb_speed.currentText()} MB/s")
        resume = (tr("断点续传开启", "resume on") if self.cb_resume.isChecked()
                  else tr("断点续传关闭", "resume off"))
        subtitles = (tr("同时下载字幕", "with subtitles") if self.cb_download_sub.isChecked()
                     else tr("不下载字幕", "no subtitles"))
        self.lb_settings_summary.setText(f"{resume} / {subtitles} / {speed}")

    def _move(self, delta):
        if self._task_rows or self._submitting:
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
        if self._task_rows or self._submitting:
            return
        for row in sorted(
                {i.row() for i in self.lst_queue.selectedIndexes()},
                reverse=True):
            self.lst_queue.takeItem(row)
            if row < len(self._queue):
                self._queue.pop(row)
        self._update_count()

    def _clear_queue(self):
        if not self._queue or self._task_rows or self._submitting:
            return
        from qfluentwidgets import MessageBox
        box = MessageBox(tr("清空下载队列", "Clear download queue"),
                         tr("确定移除队列中的全部链接吗？", "Remove all links from the queue?"), self)
        box.yesButton.setText(tr("清空", "Clear"))
        box.cancelButton.setText(tr("取消", "Cancel"))
        if not box.exec():
            return
        self.lst_queue.clear()
        self._queue.clear()
        self._update_count()

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, tr("选择保存目录", "Pick save folder"),
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
        self._submitting = True
        self._sync_queue_controls()
        try:
            self._submit_downloads()
        finally:
            self._submitting = False
            self._sync_queue_controls()
            self.cb_quality.setEnabled(bool(self._qualities) and not self._task_rows)

    def _submit_downloads(self):
        if not self._queue:
            toast.show_warning(self, tr("请先添加下载链接", "Add download links first"))
            return
        if not self.services.ffmpeg_ready():
            toast.show_error(self, tr("FFmpeg 未就绪，请稍后重试", "FFmpeg not ready"))
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
            headers = _parse_headers(self.ed_headers.text().strip())
        except ValueError as exc:
            toast.show_warning(self, str(exc))
            return
        self.save_prefs()

        speed_str = self.cb_speed.currentText()
        base_params = {
            "threads": int(self.cb_threads.currentText()),
            "output_format": self.cb_format.currentText(),
            "speed_limit": 0 if speed_str == tr("不限", "Unlimited") else int(speed_str),
            "cookie": self.ed_cookie.text().strip() or None,
            "proxy": proxy,
            "headers": headers,
            "resume": self.cb_resume.isChecked(),
            "download_sub": self.cb_download_sub.isChecked(),
            "notify": self.cb_notify.isChecked(),
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
        from core.m3u8_downloader import M3U8Downloader
        for i, item in enumerate(self._queue):
            name = _safe_download_name(item["name"])
            output_path = os.path.join(
                out_dir, f"{name}.{base_params['output_format']}")
            output_key = os.path.normcase(os.path.abspath(output_path))
            if os.path.exists(output_path) or output_key in reserved:
                base, ext = os.path.splitext(output_path)
                counter = 1
                while (os.path.exists(f"{base}_{counter}{ext}")
                       or os.path.normcase(os.path.abspath(
                           f"{base}_{counter}{ext}")) in reserved):
                    counter += 1
                output_path = f"{base}_{counter}{ext}"
            reserved.add(os.path.normcase(os.path.abspath(output_path)))
            p = dict(base_params)
            p["url"] = item["url"]
            p["master_url"] = item.get("master_url", item["url"])
            p["name"] = name
            p["index"] = i
            downloader = M3U8Downloader()
            tid = mgr.add_task(
                name=f"{tr('M3U8下载', 'M3U8 Download')} - {name}", task_type="m3u8",
                file_path="", output_path=output_path, params=p,
                runner=partial(self._runner, downloader),
                canceller=downloader.cancel,
                history_type=tr("M3U8 下载", "M3U8 Download"), history_target=tr("M3U8下载", "M3U8 Download"),
                need_ffmpeg=True,
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
            toast.show_error(self, tr("任务提交失败：FFmpeg 未就绪", "Submit failed: FFmpeg not ready"))

    def _runner(self, downloader, task, prog):
        p = task.params
        ok = downloader.download(
            p.get("url", ""), task.output_path, prog,
            threads=p.get("threads", 16), cookie=p.get("cookie"),
            headers=p.get("headers"), proxy=p.get("proxy"),
            speed_limit=p.get("speed_limit", 0),
            resume=p.get("resume", True),
            output_format=p.get("output_format", "mp4"),
            history_url=p.get("master_url"))
        # 字幕下载（对齐 tkinter 版 _run_task 的 m3u8 分支）
        if ok and p.get("download_sub"):
            try:
                subs = downloader.get_subtitles(
                    p.get("master_url", p.get("url", "")),
                    headers=p.get("headers"), cookie=p.get("cookie"),
                    proxy=p.get("proxy"))
                if subs:
                    for sub in subs:
                        sub_url = sub["url"]
                        lang = _safe_subtitle_lang(sub.get("lang", "und"))
                        ext = ".vtt" if ".vtt" in sub_url.lower() else ".srt"
                        sub_out = (os.path.splitext(task.output_path)[0]
                                   + f".{lang}{ext}")
                        sub_ok = downloader.download_subtitle(
                            sub_url, sub_out, cookie=p.get("cookie"),
                            headers=p.get("headers"), proxy=p.get("proxy"))
                        prog(-1, tr("字幕{}: ", "Subtitle {}: ").format(tr("已保存", "saved") if sub_ok else tr("下载失败", "failed"))
                                 + f"{os.path.basename(sub_out)}")
                else:
                    prog(-1, tr("未找到字幕轨道（该视频可能没有字幕）", "No subtitle track found (video may have none)"))
            except Exception as e:  # noqa: BLE001
                prog(-1, tr("字幕下载出错: {}", "Subtitle download error: {}").format(e))
        if ok and p.get("notify"):
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            except (ImportError, OSError):
                pass
        return ok

    def _cancel_all(self):
        mgr = self.services.task_manager
        for tid in list(self._task_rows):
            mgr.cancel_task(tid)
        self.action_bar.btn_cancel.setEnabled(False)

    # ── 进度/状态联动 ────────────────────────────
    _STATE_TO_QUEUE = {
        tm.WAITING: "waiting", tm.PAUSED: "waiting",
        tm.RUNNING: "running", tm.SUCCESS: "success",
        tm.FAILED: "failed", tm.CANCELLED: "cancelled",
    }

    def _on_progress(self, task_id, pct, msg, speed):
        if task_id not in self._task_rows:
            return
        self.action_bar.set_status(msg)
        if pct >= 0:
            self.action_bar.set_total(pct)
        # 行级实时状态：运行中蓝点 + 进度% + 速度（对标 Fluent-M3U8 任务卡）
        row = self._task_rows[task_id]
        extra = ""
        if pct >= 0:
            extra = f"{int(pct)}%"
        if speed:
            extra = f"{extra}  {speed}".strip()
        self._set_row_state(row, "running", extra)

    def _on_state(self, task_id, state):
        if task_id not in self._task_rows:
            return
        row = self._task_rows[task_id]
        if 0 <= row < len(self._queue):
            self._set_row_state(
                row, self._STATE_TO_QUEUE.get(state, "waiting"))
            # 失败即时提示（具体 URL 队列项 + 原因），与面板级失败 toast 一致
            if state == tm.FAILED:
                task = self.services.task_manager.get_task(task_id)
                reason = (task.error if task and task.error
                          else tr("未知错误", "unknown error"))
                display = self._queue[row]["display"]
                toast.show_error(
                    self,
                    tr("下载失败：{}", "Download failed: {}").format(display) +
                    tr("（{}）", " ({})").format(reason))
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
            self.cb_quality.setEnabled(bool(self._qualities) and not self._task_rows)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 820
        direction = (QBoxLayout.TopToBottom if narrow
                     else QBoxLayout.LeftToRight)
        for layout in (self.url_actions, self.quality_layout,
                       self.path_layout, self.option_layout):
            layout.setDirection(direction)
        self.queue_controls.layout().setDirection(
            QBoxLayout.TopToBottom if event.size().width() < 1100
            else QBoxLayout.LeftToRight)
        self.settings_grid.set_columns(1 if narrow else 2)
        self.adv_grid.set_columns(1 if narrow else 2)
        for edit, wide_width in self._responsive_edits:
            if narrow:
                edit.setMinimumWidth(0)
                edit.setMaximumWidth(16777215)
            else:
                edit.setFixedWidth(wide_width)
        self._refresh_responsive_layout()
        QTimer.singleShot(0, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        """方向切换后清除宽屏 sizeHint，避免窄窗口出现横向滚动。"""
        for layout in (self.url_actions, self.quality_layout,
                       self.path_layout, self.queue_controls.layout(),
                       self.settings_grid, self.adv_grid):
            layout.invalidate()
            layout.activate()
        self.content_layout.invalidate()
        self.content_layout.activate()
        self.content.setMinimumWidth(0)
        self.content.updateGeometry()

    def closeEvent(self, event):
        """关闭页面时隔离仍在联网的解析线程，避免线程随控件销毁而崩溃。"""
        worker = self._worker
        self._worker = None
        if worker is not None and worker.isRunning():
            worker.stop()
            if not worker.wait(500):
                for signal in (worker.sig_done, worker.sig_fail,
                               worker.sig_error, worker.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                worker.setParent(None)
                _ORPHAN_QUALITY_WORKERS.add(worker)
                worker.finished.connect(
                    lambda current=worker: _release_orphan_worker(current))
        super().closeEvent(event)

    # ── 参数/偏好（10 键与 tkinter 版一致）────────
    def collect_params(self) -> dict:
        return {
            "url": self.txt_url.toPlainText().strip(),
            "quality": self.cb_quality.currentText(),
            "name": self.ed_name.text(),
            "out_dir": self.ed_dir.text(),
            "threads": self.cb_threads.currentText(),
            "format": self.cb_format.currentText(),
            "speed": self.cb_speed.currentText(),
            "cookie": self.ed_cookie.text(),
            "proxy": self.ed_proxy.text(),
            "headers": self.ed_headers.text(),
            "resume": self.cb_resume.isChecked(),
            "download_sub": self.cb_download_sub.isChecked(),
            "notify": self.cb_notify.isChecked(),
        }

    def collect_prefs(self) -> dict:
        return {
            "out_dir": self.ed_dir.text(),
            "threads": self.cb_threads.currentText(),
            "format": self.cb_format.currentText(),
            "speed": self.cb_speed.currentText(),
            "resume": self.cb_resume.isChecked(),
            "notify": self.cb_notify.isChecked(),
            "download_sub": self.cb_download_sub.isChecked(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("out_dir"):
            self.ed_dir.setText(prefs["out_dir"])
        if prefs.get("threads") in THREADS_VALUES:
            self.cb_threads.setCurrentText(prefs["threads"])
        if prefs.get("format") in FORMAT_VALUES:
            self.cb_format.setCurrentText(prefs["format"])
        if prefs.get("speed") in SPEED_VALUES:
            self.cb_speed.setCurrentText(prefs["speed"])
        # Cookie、代理和请求头可能包含凭据，不从旧偏好恢复到界面。
        if "resume" in prefs:
            self.cb_resume.setChecked(bool(prefs["resume"]))
        if "notify" in prefs:
            self.cb_notify.setChecked(bool(prefs["notify"]))
        if "download_sub" in prefs:
            self.cb_download_sub.setChecked(bool(prefs["download_sub"]))
