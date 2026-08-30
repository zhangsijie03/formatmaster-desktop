"""doc_panel — 文档转换面板（阶段2 迁移自 gui/panels/doc_panel.py）。

PDF · Word · Excel · PPT · WPS · TXT · 图片 · Markdown · EPUB · RTF · ODT
互转。添加文件后按整批扩展名计算 DOC_CONVERSION_MAP 的共同目标；
任务经 TaskManager 通用链路执行 core.doc_converter（不依赖 FFmpeg）。
"""
import os

from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PushButton

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow
from utils.config import DOC_CONVERSION_MAP, DOC_READ_FORMATS

DOC_EXTS = set(DOC_READ_FORMATS.keys())
PLACEHOLDER = tr("请先添加文件", "Add files first")
HTML_EXTS = frozenset({".html", ".htm"})


def _format_name(ext):
    """转换格式的稳定展示名；业务层始终使用扩展名。"""
    names = {
        ".pdf": "PDF", ".docx": "Word", ".doc": "Word 97",
        ".wps": "WPS Writer", ".xlsx": "Excel", ".xls": "Excel 97",
        ".et": "WPS Spreadsheets", ".csv": "CSV", ".pptx": "PowerPoint",
        ".ppt": "PowerPoint 97", ".dps": "WPS Presentation", ".txt": "TXT",
        ".html": "HTML", ".htm": "HTML", ".jpg": "JPG", ".jpeg": "JPEG",
        ".png": "PNG", ".bmp": "BMP", ".tiff": "TIFF", ".webp": "WEBP",
        ".md": "Markdown", ".epub": "EPUB", ".rtf": "RTF", ".odt": "ODT",
        ".ofd": "OFD",
    }
    return names.get(ext, ext.lstrip(".").upper())


class DocPanelPage(BaseQtPanel, TaskPanelMixin):
    """文档转换页。"""

    panel_key = "document"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("文档转换", "Document conversion"),
            tr("添加文档，自动匹配整批文件可用的输出格式",
               "Add documents to find output formats supported by the whole batch"),
            FluentIcon.DOCUMENT))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=DOC_EXTS)
        lay.addWidget(self.file_card)

        # 转换设置
        sec = FormSection(tr("转换参数", "Conversion settings"), FluentIcon.SETTING)
        self.params_grid = FormGrid(columns=2)
        self.source_label = CaptionLabel(PLACEHOLDER)
        self.source_label.setWordWrap(True)
        self.source_label.setProperty("sec", True)
        self.params_grid.add_field(
            tr("已识别格式", "Detected formats"),
            self.source_label,
            hint=tr("添加文件后自动识别整批文件",
                    "Detected automatically for the whole batch"))
        self.cb_tgt = ComboBox()
        self.cb_tgt.addItems([PLACEHOLDER])
        self.cb_tgt.setCurrentIndex(0)
        self.cb_tgt.setEnabled(False)
        self.cb_tgt = self.params_grid.add_field(
            tr("目标格式", "Target format"), self.cb_tgt,
            hint=tr("只展示整批文件都支持的转换格式",
                    "Only targets supported by every file are shown"))
        sec.add_form(self.params_grid)
        self.target_hint = CaptionLabel(
            tr("添加文件后显示可转换格式；每个文件分别输出，不会合并。",
               "Add files to see available formats. Each file is converted separately, not merged."))
        self.target_hint.setWordWrap(True)
        sec.add_widget(self.target_hint)

        # HTML 预览是输入类型的上下文操作，仅在批次中有网页时出现。
        self.preview_row = QWidget()
        act_row = QHBoxLayout(self.preview_row)
        act_row.setContentsMargins(0, 0, 0, 0)
        act_row.setSpacing(8)
        self.btn_preview_html = PushButton(FluentIcon.VIEW,
                                           tr("预览 HTML", "Preview HTML"))
        self.btn_preview_html.setToolTip(
            tr("内置 Chromium 预览，适合 HTML/网页文件", "Built-in preview for HTML files"))
        self.btn_preview_html.clicked.connect(self._preview_html)
        self.btn_preview_html.setEnabled(False)
        act_row.addWidget(self.btn_preview_html)
        self.preview_source_label = CaptionLabel()
        self.preview_source_label.setWordWrap(True)
        act_row.addWidget(self.preview_source_label, 1)
        self.preview_row.hide()
        sec.add_widget(self.preview_row)
        lay.addWidget(sec)

        # 输出目录
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始转换", "Convert"))
        lay.addWidget(self.action_bar)

        self.file_card.files_changed.connect(self._on_files_changed)
        self.file_card.table.itemSelectionChanged.connect(self._sync_preview_source)
        self.cb_tgt.currentIndexChanged.connect(self._on_target_changed)
        self._target_exts = []
        self._wire_tasks()

    # ── HTML 预览（QtWebEngine）──────────────
    def _preview_html(self):
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加文件", "Add files first"))
            return
        fp = self._selected_html_file()
        if not fp:
            toast.show_warning(self, tr("仅支持预览 HTML 文件", "Only HTML files can be previewed"))
            return
        from gui_qt.components.html_preview import HtmlPreviewDialog
        dlg = HtmlPreviewDialog(fp, self.window())
        dlg.exec()

    def _selected_html_file(self) -> str:
        """优先预览所选网页；未选网页时沿用批次中第一份网页。"""
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        for index in rows:
            if 0 <= index.row() < len(files):
                path = files[index.row()]
                if os.path.splitext(path)[1].lower() in HTML_EXTS:
                    return path
        return next((path for path in files
                     if os.path.splitext(path)[1].lower() in HTML_EXTS), "")

    def _sync_preview_source(self) -> None:
        """显示按钮实际将打开的源文件，移除或切换选择后不保留旧文件名。"""
        path = self._selected_html_file()
        self.preview_row.setVisible(bool(path))
        self.btn_preview_html.setEnabled(bool(path))
        self.preview_source_label.setText(os.path.basename(path))
        self.preview_source_label.setToolTip(path)

    # ── 整批格式识别 ──
    def _detect(self):
        """自动计算整批文件的共同目标，避免只检查第一项。"""
        files = self.file_card.files()
        if not files:
            self._reset_detection()
            return

        source_exts = []
        target_lists = []
        for path in files:
            ext = os.path.splitext(path)[1].lower()
            src = DOC_READ_FORMATS.get(ext)
            if not src:
                continue
            if ext not in source_exts:
                source_exts.append(ext)
            target_lists.append(DOC_CONVERSION_MAP.get(src, []))

        common = list(target_lists[0]) if target_lists else []
        for targets in target_lists[1:]:
            common = [ext for ext in common if ext in targets]

        previous = self._selected_target_ext()
        self.cb_tgt.clear()
        self._target_exts = common
        if common:
            self.cb_tgt.addItems(
                [f"{_format_name(ext)}  ({ext})" for ext in common])
            self.cb_tgt.setEnabled(True)
            self.cb_tgt.setCurrentIndex(
                common.index(previous) if previous in common else 0)
        else:
            self.cb_tgt.addItems([tr("无共同可转换格式", "No common target format")])
            self.cb_tgt.setCurrentIndex(0)
            self.cb_tgt.setEnabled(False)

        source_text = " · ".join(_format_name(ext) for ext in source_exts)
        self.source_label.setText(
            tr("{} · {} 个文件", "{} · {} files").format(
                source_text, len(files)))
        self._sync_preview_source()
        self._on_target_changed()
        self._sync_start_enabled()

    def _reset_detection(self):
        self._target_exts = []
        self.cb_tgt.clear()
        self.cb_tgt.addItems([PLACEHOLDER])
        self.cb_tgt.setCurrentIndex(0)
        self.cb_tgt.setEnabled(False)
        self.source_label.setText(PLACEHOLDER)
        self._sync_preview_source()
        self._on_target_changed()

    def _selected_target_ext(self):
        index = self.cb_tgt.currentIndex()
        if 0 <= index < len(self._target_exts):
            return self._target_exts[index]
        return ""

    def _on_target_changed(self, *_args):
        ext = self._selected_target_ext()
        self.file_card.set_target_fmt(ext.lstrip(".").upper() if ext else "")
        # 共同目标为空不代表文件损坏，明确引导用户按源格式分批处理。
        if not self.file_card.files():
            message = tr("添加文件后显示可转换格式；每个文件分别输出，不会合并。",
                         "Add files to see available formats. Each file is converted separately, not merged.")
        elif not ext:
            message = tr("这批文件没有共同目标格式，请按源格式分批转换。",
                         "These files have no common output format. Convert them in batches grouped by source format.")
        else:
            message = tr("整批可选 {} 种目标格式；每个文件分别输出，不会合并。",
                         "{} output formats are available for this batch. Each file is converted separately, not merged.").format(len(self._target_exts))
        self.target_hint.setText(message)

    def _on_files_changed(self):
        self._detect()

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "target": self._selected_target_ext(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        # 与 tkinter 版一致：不持久化 target（需重新检测）
        return {
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        return self.services.doc_conv.convert(
            task.file_path, task.output_path, prog)

    def _make_task(self, f):
        ext = self._selected_target_ext()
        if not ext:
            toast.show_warning(self, tr("当前文件没有共同的可转换格式",
                                        "These files have no common target format"))
            return None
        src = DOC_READ_FORMATS.get(os.path.splitext(f)[1].lower())
        if ext not in DOC_CONVERSION_MAP.get(src, []):
            toast.show_warning(
                self,
                tr("文件 {} 不支持转换为 {}",
                   "{} cannot be converted to {}").format(
                    os.path.basename(f), _format_name(ext)))
            return None
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(f, out_dir, ext)
        return dict(
            name=f"{tr('文档转换', 'Doc Convert')} - {os.path.basename(f)}",
            task_type="doc", file_path=f, output_path=out_path,
            params=params, runner=self._runner,
            canceller=self.services.doc_conv.cancel,
            history_type=tr("文档转换", "Document Convert"), history_target=ext.lstrip(".").upper(),
            need_ffmpeg=False)

    def _start(self):
        self._submit_files()

    def _empty_hint(self):
        if self.file_card.files() and not self._selected_target_ext():
            return tr("当前文件没有共同的可转换格式",
                      "These files have no common target format")
        return tr("请先添加要转换的文档文件", "Add documents to convert first")

    def _sync_start_enabled(self):
        """除通用任务状态外，文档批次还必须存在共同目标。"""
        TaskPanelMixin._sync_start_enabled(self)
        enabled = (self.action_bar.btn_go.isEnabled()
                   and bool(self._selected_target_ext()))
        self.action_bar.btn_go.setEnabled(enabled)
        if not enabled:
            self.action_bar.btn_go.setToolTip(self._empty_hint())

    def resizeEvent(self, event):
        """窄窗口下参数改为单列，防止格式名和辅助文案被挤压。"""
        super().resizeEvent(event)
        if hasattr(self, "params_grid"):
            self.params_grid.set_columns(
                1 if self.viewport().width() < 820 else 2)
