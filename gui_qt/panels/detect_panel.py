"""detect_panel — 格式检测面板（阶段2 迁移自 gui/panels/detect_panel.py + main.py 检测逻辑）。

批量检测文件夹中所有文件的格式：扩展名归类 + 文件头魔数内容识别，
结果按类别分组展示（勾选/全选），支持自动添加到对应面板与批量转换。
扫描在后台线程执行，UI 经 Qt 信号更新。
"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QTableWidget, QTableWidgetItem,
                               QWidget)
from qfluentwidgets import (CaptionLabel, CheckBox,
                            LineEdit, PushButton, FluentIcon)

from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.components.safe_worker import SafeWorker
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import ActionBar, ActionStatusState

VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.wmv', '.mov', '.flv', '.webm', '.ts', '.3gp'}
AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.amr', '.opus'}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.svg'}
DOC_EXTS = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.rtf'}
PDF_EXTS = {'.pdf'}

TYPE_ORDER = ['video', 'audio', 'image', 'doc', 'pdf', 'other']
TYPE_ICONS = {'video': '🎬', 'audio': '🎵', 'image': '🖼️',
              'doc': '📄', 'pdf': '📕', 'other': '📁'}
TYPE_NAMES = {
    'video': tr('视频文件', 'Video'), 'audio': tr('音频文件', 'Audio'),
    'image': tr('图片文件', 'Image'), 'doc': tr('文档文件', 'Document'),
    'pdf': tr('PDF 文件', 'PDF'), 'other': tr('其他文件', 'Other'),
}
# 类别 → Qt 面板页 key（批量转换/自动添加目标）
CAT_PAGE = {'video': 'video', 'audio': 'audio', 'image': 'image',
            'doc': 'document', 'pdf': 'pdf'}


def _fmt_size(n):
    if n <= 0:
        return "--"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _file_count_text(count):
    """英文分组标题区分单复数，中文保持统一计数格式。"""
    if count == 1:
        return tr("{} 个文件", "{} file").format(count)
    return tr("{} 个文件", "{} files").format(count)


def classify_ext(fp):
    """按扩展名归类文件。"""
    ext = os.path.splitext(fp)[1].lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in DOC_EXTS:
        return 'doc'
    if ext in PDF_EXTS:
        return 'pdf'
    return 'other'


def detect_format_by_content(fp):
    """通过文件头及容器结构检测实际格式；无法确定时返回 None。"""
    try:
        with open(fp, 'rb') as f:
            header = f.read(512)
    except OSError:
        return None
    if not header or len(header) < 4:
        return None
    if header[:4] == b'%PDF':
        return 'pdf'
    if header[:2] == b'\xff\xd8':
        return 'image'
    if header[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image'
    if header[:3] == b'GIF':
        return 'image'
    if header[:2] == b'BM':
        return 'image'
    if header[:4] == b'RIFF' and len(header) >= 12:
        if header[8:12] == b'WEBP':
            return 'image'
        if header[8:12] == b'AVI ':
            return 'video'
        if header[8:12] == b'WAVE':
            return 'audio'
    if header[:4] in (b'II*\x00', b'MM\x00*'):
        return 'image'
    if len(header) >= 12 and header[4:8] == b'ftyp':
        brand = header[8:12].lower()
        if brand in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'):
            return 'image'
        if brand in (b'm4a ', b'm4b ', b'f4a '):
            return 'audio'
        return 'video'
    if header[:4] == b'\x1aE\xdf\xa3':
        return 'video'
    if header[:4] == b'\x30\x26\xb2\x75':
        ext = os.path.splitext(fp)[1].lower()
        return 'audio' if ext in ('.wma',) else 'video'
    if header[:3] == b'\x00\x00\x00' and len(header) > 3 and header[3] in (0x18, 0x1C, 0x20):
        return 'video'
    if header[:3] == b'ID3':
        return 'audio'
    if header[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2',
                      b'\xff\xf1', b'\xff\xf9'):
        return 'audio'
    if header[:4] == b'fLaC':
        return 'audio'
    if header[:4] == b'OggS':
        return 'audio'
    if header[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return 'doc'
    if header[:4] == b'PK\x03\x04':
        try:
            import zipfile
            with zipfile.ZipFile(fp) as archive:
                names = set(archive.namelist())
            if any(name.startswith(('word/', 'xl/', 'ppt/')) for name in names):
                return 'doc'
        except (OSError, zipfile.BadZipFile):
            return None
        return 'other'
    if b'<svg' in header.lower():
        return 'image'
    return None


class _ScanWorker(SafeWorker):
    """后台扫描文件夹：归类 + 内容识别（异常由 SafeWorker 统一兜底）。"""

    sig_progress = Signal(int, int)           # (当前, 总数)
    sig_done = Signal(dict, list)             # (detected, file_info)

    def __init__(self, path, stop_flag, parent=None):
        super().__init__(parent)
        self._path = path
        self._stop = stop_flag  # 共享 list，[0]=True 表示取消

    def work(self):
        detected = {k: [] for k in TYPE_ORDER}
        all_files = []
        for root, dirs, files in os.walk(self._path):
            if self._stop[0] or self.is_stopped():
                self.sig_done.emit({}, [])
                return
            dirs.sort()
            for f in sorted(files):
                all_files.append(os.path.join(root, f))
        total = len(all_files)
        file_info = []
        for i, fp in enumerate(all_files):
            if self._stop[0] or self.is_stopped():
                self.sig_done.emit({}, [])
                return
            extension_cat = classify_ext(fp)
            content_cat = detect_format_by_content(fp)
            # 内容证据优先，避免伪装扩展名的文件被送入错误转换器。
            cat = content_cat or extension_cat
            detected[cat].append(fp)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            file_info.append((fp, cat, _fmt_size(size),
                              extension_cat, content_cat))
            if (i + 1) % 20 == 0 or i == total - 1:
                self.sig_progress.emit(i + 1, total)
        self.sig_done.emit(detected, file_info)


class DetectPanelPage(BaseQtPanel):
    """格式检测页。"""

    panel_key = "format_detect"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("格式检测", "Format Detection"),
            tr("按文件内容识别真实格式，发现扩展名不一致，并将文件发送到对应转换页面",
               "Identify formats by content, flag extension mismatches, and send files to matching converters"),
            FluentIcon.SEARCH)
        lay.addWidget(self.header)

        card = FormSection(tr("检测设置", "Detect settings"), FluentIcon.SEARCH)
        self.ed_path = LineEdit()
        self.ed_path.setPlaceholderText(tr("选择要扫描的文件夹…", "Pick a folder to scan…"))
        self.ed_path.setAccessibleName(tr("目标文件夹", "Target folder"))
        self.btn_browse = PushButton(tr("浏览", "Browse"))
        self.btn_browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(8)
        path_row.addWidget(self.ed_path, 1)
        path_row.addWidget(self.btn_browse)
        path_wrap = QWidget()
        path_wrap.setLayout(path_row)
        settings_grid = FormGrid(columns=1)
        settings_grid.add_field(
            tr("目标文件夹", "Target folder"), path_wrap,
            hint=tr("将递归扫描此文件夹及所有子文件夹",
                    "Scans this folder and all subfolders recursively"))
        card.add_form(settings_grid)
        self.scan_hint = CaptionLabel(tr(
            "会递归扫描所有子文件夹，并优先按文件内容判断真实格式。检测只读取文件，不会修改或移动源文件。",
            "All subfolders are scanned. File content takes priority when identifying the true format. Detection is read-only and never moves source files."))
        self.scan_hint.setWordWrap(True)
        card.add_widget(self.scan_hint)
        self.cb_auto_add = CheckBox(
            tr("检测完成后自动添加到对应转换页面（不自动开始转换）",
               "After detection, add to matching converters without starting"))
        self.cb_auto_add.setChecked(True)
        card.add_widget(self.cb_auto_add)
        self.routing_hint = CaptionLabel(tr(
            "格式不一致和未识别文件不会自动加入转换队列，需先人工核对。",
            "Files with mismatched or unknown formats are not routed automatically and should be reviewed first."))
        self.routing_hint.setWordWrap(True)
        card.add_widget(self.routing_hint)
        lay.addWidget(card)

        lay.addWidget(self._build_result_card())

        # 双态主操作仍由同一个按钮承担，但进入全局标题命令区，避免与
        # 标准转换页面产生两套操作位置。
        self.action_bar = ActionBar(tr("开始检测", "Detect"), self)
        self.btn_go = self.action_bar.btn_go
        self.btn_go.setIcon(FluentIcon.SEARCH)
        self.btn_go.clicked.connect(self._on_go)
        self.btn_cancel = self.action_bar.btn_cancel
        self.btn_cancel.clicked.connect(self._stop_scan)
        self.lb_status = self.action_bar.status_label
        lay.addWidget(self.action_bar)

        self._worker = None
        self._stop_flag = [False]
        self._phase = "idle"          # idle / scanning / result
        self._rows = []               # [(file_path, cat, check_item)]
        self._result_total = 0
        self._result_review = 0
        self._result_unknown = 0

    def _build_result_card(self):
        card = FormSection(tr("检测结果", "Result"), FluentIcon.INFO)

        self.result_summary = CaptionLabel(tr(
            "尚未检测。选择文件夹后开始检测，结果将按文件类型分组。",
            "No scan yet. Choose a folder to group detected files by type."))
        self.result_summary.setWordWrap(True)
        card.add_widget(self.result_summary)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addStretch(1)
        self.btn_sel_all = PushButton(tr("全选", "Select all"))
        self.btn_sel_all.clicked.connect(lambda: self._set_all(True))
        self.btn_unsel = PushButton(tr("取消全选", "Deselect all"))
        self.btn_unsel.clicked.connect(lambda: self._set_all(False))
        self.btn_reset = PushButton(tr("重新检测", "Re-scan"))
        self.btn_reset.clicked.connect(self._reset)
        for b in (self.btn_sel_all, self.btn_unsel, self.btn_reset):
            b.setEnabled(False)
            head.addWidget(b)
        head_wrap = QWidget()
        head_wrap.setLayout(head)
        card.add_widget(head_wrap)

        self.table = QTableWidget(0, 5, card)
        self.table.setHorizontalHeaderLabels(["✓", tr("文件名", "Name"), tr("大小", "Size"), tr("扩展名", "Ext"), tr("类型", "Type")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(260)
        self.table.setAlternatingRowColors(True)
        self.table.setAccessibleName(tr("格式检测结果", "Format detection results"))
        self.table.itemChanged.connect(self._on_item_changed)
        card.add_widget(self.table)
        return card

    # ── 交互 ─────────────────────────────────────
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("选择文件夹", "Pick folder"),
                                             self.ed_path.text() or "")
        if d:
            self.ed_path.setText(d)

    def _on_go(self):
        if self._phase == "result":
            self._batch_convert()
        else:
            self._start_scan()

    def _start_scan(self):
        path = self.ed_path.text().strip()
        if not path or not os.path.isdir(path):
            toast.show_warning(self, tr("请选择有效的文件夹", "Pick a valid folder"))
            return
        self.save_prefs()
        self._clear_table()
        self._stop_flag = [False]
        self._phase = "scanning"
        self._set_scan_controls(False)
        self.action_bar.set_running(True)
        self.action_bar.set_status(
            tr("正在扫描文件夹…", "Scanning folder…"),
            ActionStatusState.RUNNING)
        self._worker = _ScanWorker(path, self._stop_flag, self)
        self._worker.sig_progress.connect(self._on_scan_progress)
        self._worker.sig_done.connect(self._on_scan_done)
        self._worker.sig_error.connect(self._on_scan_error)
        self._worker.start()

    def _stop_scan(self):
        self._stop_flag[0] = True
        if self._worker is not None:
            self._worker.stop()
        self.btn_cancel.setEnabled(False)
        self.action_bar.set_status(
            tr("正在取消…", "Cancelling…"), ActionStatusState.WARNING)

    def _on_scan_progress(self, cur, total):
        self.action_bar.set_status(
            tr("正在检测 {}/{} 个文件", "Detecting {}/{} files")
            .format(cur, total), ActionStatusState.RUNNING)
        self.action_bar.set_total(int(cur * 100 / max(1, total)))

    def _on_scan_done(self, detected, file_info):
        self._finish_worker()
        self._set_scan_controls(True)
        self.action_bar.set_running(False)
        if not file_info:
            if self._stop_flag[0]:
                self.action_bar.set_status(
                    tr("检测已取消", "Scan cancelled"),
                    ActionStatusState.WARNING)
            else:
                self.action_bar.set_status(
                    tr("文件夹为空，未检测到文件",
                       "Folder is empty, no files found"),
                    ActionStatusState.WARNING)
            self._phase = "idle"
            return
        self._show_results(detected, file_info)

    def _on_scan_error(self, message):
        """后台异常必须恢复可操作状态，不能让页面永久停在扫描中。"""
        self._finish_worker()
        self._set_scan_controls(True)
        self.action_bar.set_running(False)
        self.action_bar.set_status(
            tr("检测失败，请检查文件夹权限", "Detection failed; check folder permissions"),
            ActionStatusState.ERROR)
        self._phase = "idle"
        toast.show_error(
            self, tr("格式检测失败：{}", "Format detection failed: {}").format(message))

    def _finish_worker(self):
        worker = self._worker
        self._worker = None
        if worker is not None:
            if worker.isRunning():
                worker.finished.connect(worker.deleteLater)
            else:
                worker.deleteLater()

    def _set_scan_controls(self, enabled):
        self.ed_path.setEnabled(enabled)
        self.btn_browse.setEnabled(enabled)
        self.cb_auto_add.setEnabled(enabled)

    # ── 结果展示 ─────────────────────────────────
    def _clear_table(self):
        self.table.itemChanged.disconnect(self._on_item_changed)
        self.table.setRowCount(0)
        self._rows = []
        self.table.itemChanged.connect(self._on_item_changed)

    def _show_results(self, detected, file_info):
        self._clear_table()
        info_map = {fi[0]: fi for fi in file_info}
        processable = {}

        for cat in TYPE_ORDER:
            files = detected.get(cat, [])
            if not files:
                continue
            # 分组标题行
            r = self.table.rowCount()
            self.table.insertRow(r)
            hdr = QTableWidgetItem(
                f"{TYPE_ICONS[cat]} {TYPE_NAMES[cat]} ({_file_count_text(len(files))})")
            f = hdr.font()
            f.setBold(True)
            hdr.setFont(f)
            self.table.setItem(r, 1, hdr)
            self.table.setSpan(r, 1, 1, 4)
            for fp in files:
                info = info_map.get(fp)
                size_str = info[2] if info else _fmt_size(os.path.getsize(fp))
                extension_type = info[3] if info else classify_ext(fp)
                content_type = info[4] if info else None
                mismatch = bool(content_type and content_type != extension_type)
                fn = os.path.basename(fp)
                if mismatch:
                    fn += f"  ⚠️ {tr('扩展名与内容不一致', 'extension/content mismatch')}"
                r = self.table.rowCount()
                self.table.insertRow(r)
                chk = QTableWidgetItem()
                if cat == 'other' or mismatch:
                    chk.setFlags(Qt.NoItemFlags)
                    chk.setCheckState(Qt.Unchecked)
                else:
                    chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                    chk.setCheckState(Qt.Checked)
                    processable.setdefault(cat, []).append(fp)
                self.table.setItem(r, 0, chk)
                self.table.setItem(r, 1, QTableWidgetItem(fn))
                self.table.setItem(r, 2, QTableWidgetItem(size_str))
                self.table.setItem(r, 3, QTableWidgetItem(
                    os.path.splitext(fp)[1].upper() or "?"))
                type_text = TYPE_NAMES[cat]
                if mismatch:
                    type_text = tr("{}（内容识别）", "{} (content)").format(type_text)
                self.table.setItem(r, 4, QTableWidgetItem(type_text))
                self._rows.append((fp, cat, chk))

        self._phase = "result"
        total_found = sum(len(files) for files in processable.values())
        self._result_total = len(file_info)
        self._result_review = sum(
            1 for _fp, _cat, _size, extension_cat, content_cat in file_info
            if content_cat and content_cat != extension_cat)
        self._result_unknown = sum(
            1 for _fp, cat, _size, _extension_cat, _content_cat in file_info
            if cat == "other")
        self.btn_go.setText(tr("批量转换选中 ({})", "Convert selected ({})").format(total_found))
        self.btn_go.setEnabled(total_found > 0)
        for b in (self.btn_sel_all, self.btn_unsel, self.btn_reset):
            b.setEnabled(True)
        self.action_bar.set_status(
            tr("检测完成，共 {} 个可处理文件",
               "Scan done, {} processable files").format(total_found),
            ActionStatusState.SUCCESS)
        self._sync_result_summary()

        if self.cb_auto_add.isChecked():
            added, unavailable = self._add_to_panels(
                {k: list(v) for k, v in processable.items()}, submit=False)
            if unavailable:
                toast.show_warning(
                    self, tr("{} 个文件没有可用的转换页面", "{} files have no matching converter")
                    .format(unavailable))
            elif added:
                self.action_bar.set_status(
                    tr("检测完成，已添加 {} 个文件到对应转换页面",
                       "Detection complete; added {} files to matching converters")
                    .format(added), ActionStatusState.SUCCESS)

    def _on_item_changed(self, item):
        if item.column() != 0:
            return
        n = sum(1 for _f, _c, chk in self._rows
                if chk.checkState() == Qt.Checked)
        if self._phase == "result":
            self.btn_go.setText(tr("批量转换选中 ({})", "Convert selected ({})").format(n))
            self.btn_go.setEnabled(n > 0)
            self._sync_result_summary()

    def _sync_result_summary(self):
        """将选择状态与异常类型同时呈现，避免用户把“已检测”误解为“均可转换”。"""
        selected = sum(
            1 for _fp, _cat, chk in self._rows
            if (chk.flags() & Qt.ItemIsUserCheckable
                and chk.checkState() == Qt.Checked))
        self.result_summary.setText(tr(
            "共 {total} 个文件：已选 {selected} 个可转换文件，{review} 个格式不一致需核对，{unknown} 个未识别。",
            "Total: {total}. Selected: {selected}. Needs review: {review}. Unknown: {unknown}.").format(
                total=self._result_total, selected=selected,
                review=self._result_review, unknown=self._result_unknown))

    def _set_all(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for _f, _cat, chk in self._rows:
            if chk.flags() & Qt.ItemIsUserCheckable:
                chk.setCheckState(state)

    def _reset(self):
        self._clear_table()
        self._phase = "idle"
        self._result_total = 0
        self._result_review = 0
        self._result_unknown = 0
        self.result_summary.setText(tr(
            "尚未检测。选择文件夹后开始检测，结果将按文件类型分组。",
            "No scan yet. Choose a folder to group detected files by type."))
        self.btn_go.setText(tr("开始检测", "Detect"))
        self.btn_go.setEnabled(True)
        for b in (self.btn_sel_all, self.btn_unsel, self.btn_reset):
            b.setEnabled(False)
        self.action_bar.set_status(
            tr("就绪", "Ready"), ActionStatusState.IDLE)

    # ── 批量转换 / 自动添加 ──────────────────────
    def _selected_by_cat(self):
        grouped = {}
        for fp, cat, chk in self._rows:
            if (chk.flags() & Qt.ItemIsUserCheckable
                    and chk.checkState() == Qt.Checked):
                grouped.setdefault(cat, []).append(fp)
        return grouped

    def _add_to_panels(self, grouped, submit=False):
        """把检测结果送入对应面板；submit=True 时直接启动转换。"""
        added = 0
        unavailable = 0
        for cat, files in grouped.items():
            page_key = CAT_PAGE.get(cat)
            page = self.main_window.pages.get(page_key) if page_key else None
            card = getattr(page, "file_card", None) if page else None
            if card is None:
                unavailable += len(files)
                continue
            n = card.add_files(files)
            added += n
            if submit and files:
                try:
                    page._start()
                except Exception as ex:  # noqa: BLE001
                    toast.show_error(
                        self, tr("{}转换提交失败：{}", "{} submission failed: {}")
                        .format(TYPE_NAMES[cat], ex))
        return added, unavailable

    def _batch_convert(self):
        grouped = self._selected_by_cat()
        if not grouped:
            toast.show_warning(self, tr("请先勾选需要转换的文件", "Check files to convert first"))
            return
        _added, unavailable = self._add_to_panels(grouped, submit=True)
        requested = sum(len(files) for files in grouped.values()) - unavailable
        if requested <= 0:
            toast.show_error(
                self, tr("所选文件没有可用的转换页面", "No matching converter for selected files"))
            return
        toast.show_success(
            self, tr("已将 {} 个文件发送到对应转换页面", "Sent {} files to matching converters")
            .format(requested))
        self._reset()

    def closeEvent(self, event):
        worker = self._worker
        if worker is not None and worker.isRunning():
            self._stop_flag[0] = True
            worker.stop()
            worker.wait(1000)
            if worker.isRunning():
                # 极慢文件系统不阻塞窗口关闭；线程脱离页面后自行安全回收。
                worker.setParent(None)
                worker.finished.connect(worker.deleteLater)
                self._worker = None
        super().closeEvent(event)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {"path": self.ed_path.text().strip(),
                "auto_add": self.cb_auto_add.isChecked()}

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("path"):
            self.ed_path.setText(prefs["path"])
        if "auto_add" in prefs:
            self.cb_auto_add.setChecked(bool(prefs["auto_add"]))
