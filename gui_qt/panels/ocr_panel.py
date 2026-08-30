"""ocr_panel — OCR 文字识别面板（合并版）。

从图片或 PDF 中识别文字，支持单文件和批量处理。
- 单文件模式：显示识别结果文本区，支持复制/导出
- 批量模式：每个输入生成一个同名 .txt 结果文件
"""
import os

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout
from qfluentwidgets import (FluentIcon, CaptionLabel, ComboBox, PushButton,
                            SwitchButton, TextEdit)

from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt import task_manager as tm
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

# 当前随应用分发的 RapidOCR 模型实际为中英文通用模型。旧版列出的日文、
# 韩文等选项不会切换底层模型，保留只会制造“已支持”的错误预期。
OCR_LANGS = ["chi_sim+eng"]
OCR_LANG_VALUES = [tr("中英通用（内置模型）", "Chinese + English (built-in)")]
_LEGACY_LANGS = {"chi_sim", "eng", "jpn", "kor", "chi_tra+eng",
                 "chi_tra", "eng+chi_sim"}

OCR_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".pdf"}


class OcrPanelPage(BaseQtPanel, TaskPanelMixin):
    """OCR 识别页（合并版，支持单文件和批量）。"""

    panel_key = "ocr"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("高级 OCR", "Advanced OCR"),
            tr("PDF 全文（含扫描件/图片文本）识别为可编辑 Word 或 TXT",
               "Convert full PDF (incl. scanned/image text) to editable Word or TXT"),
            FluentIcon.FONT)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=OCR_EXTS)
        lay.addWidget(self.file_card)

        # 识别设置
        sec = FormSection(tr("识别设置", "OCR settings"), FluentIcon.FONT)
        self.settings_grid = FormGrid(columns=2)
        self.cb_lang = ComboBox()
        self.cb_lang.addItems(OCR_LANG_VALUES)
        self.cb_export = ComboBox()
        self.cb_export.addItems([tr("TXT 文本", "TXT"), tr("Word 文档", "Word")])
        self.cb_export.setCurrentIndex(0)
        self.settings_grid.add_field(
            tr("识别模型", "Recognition model"), self.cb_lang,
            hint=tr("当前内置 RapidOCR 模型支持中文和英文；不会联网下载其他语言模型",
                    "The bundled RapidOCR model supports Chinese and English; no other language models are downloaded"))
        self.settings_grid.add_field(
            tr("导出格式", "Export format"), self.cb_export,
            hint=tr("TXT 适合纯文字；Word 可重建表格并保留原页图像",
                    "TXT is text-only; Word can rebuild tables and keep page images"))
        sec.add_form(self.settings_grid)

        # 表格识别 / 嵌入原图（Word 导出时生效）
        opt_row = QHBoxLayout()
        opt_row.setSpacing(16)
        self.sw_table = SwitchButton(tr("表格识别", "Table recognition"))
        self.sw_table.setOnText(tr("表格识别", "Table recognition"))
        self.sw_table.setToolTip(
            tr("Word 导出时：把扫描页中的表格重建成可编辑的真·Word表格（每个单元格可改）；关闭则只输出纯文字",
               "Word export: rebuild scanned tables into editable real Word tables (each cell editable); off = plain text only"))
        self.sw_table.setChecked(True)
        self.sw_image = SwitchButton(tr("嵌入原图", "Keep original image"))
        self.sw_image.setOnText(tr("嵌入原图", "Keep original image"))
        self.sw_image.setToolTip(
            tr("保留每页原始图像（所见即所得，含图表/版式）；关闭则只输出可编辑的表格与文字",
               "Embed original page image (WYSIWYG, with charts/layout); off = editable tables & text only"))
        self.sw_image.setChecked(True)
        opt_row.addWidget(self.sw_table)
        opt_row.addWidget(self.sw_image)
        opt_row.addStretch(1)
        sec.add_layout(opt_row)

        # 批量模式属于识别参数，收进同一设置卡片。
        batch_row = QHBoxLayout()
        batch_row.setSpacing(8)
        self.sw_batch = SwitchButton(tr("批量模式", "Batch mode"))
        self.sw_batch.setOnText(tr("批量模式", "Batch mode"))
        self.sw_batch.setToolTip(
            tr("批量模式：每个文件生成同名结果文件（TXT/Word 按导出格式）；关闭：单文件模式，显示识别结果",
               "Batch: each file → one result file (TXT/Word by export format); Off: single file, show result"))
        self.sw_batch.checkedChanged.connect(self._on_batch_changed)
        batch_row.addWidget(self.sw_batch)
        batch_row.addStretch(1)
        sec.add_layout(batch_row)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)
        lay.addWidget(sec)

        # 识别结果区（单文件模式显示）
        self.result_card = FormSection(tr("识别结果", "Result"), FluentIcon.INFO)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addStretch(1)
        btn_copy = PushButton(tr("复制到剪贴板", "Copy to clipboard"))
        btn_copy.clicked.connect(self._copy_result)
        btn_export = PushButton(tr("导出 TXT", "Export TXT"))
        btn_export.clicked.connect(self._export_txt)
        head.addWidget(btn_copy)
        head.addWidget(btn_export)
        self.result_card.add_layout(head)
        self.txt_result = TextEdit()
        self.txt_result.setReadOnly(True)
        self.txt_result.setMinimumHeight(120)
        self.txt_result.setPlaceholderText(
            tr("识别完成后在此显示文字…", "Recognized text will show here…"))
        from gui_qt.components import design_system as _ds
        _ds.apply_text_edit_style(self.txt_result)
        self.result_card.add_widget(self.txt_result)
        self.result_hint = CaptionLabel(tr(
            "此处只预览可提取文字；Word 中的页面图像、表格和版式请打开输出文件检查。",
            "This area previews extractable text only. Open the Word output to review page images, tables, and layout."))
        self.result_hint.setWordWrap(True)
        self.result_card.add_widget(self.result_hint)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)
        # 结果预览属于处理后的反馈，放在输出规则之后更符合操作顺序。
        lay.addWidget(self.result_card)

        self.action_bar = ActionBar(tr("开始识别", "OCR"))
        lay.addWidget(self.action_bar)

        from core.ocr_batch import make_runner
        self.services.task_manager.register_runner("ocr_batch", make_runner)
        self._reserved_output_paths = set()
        self._wire_tasks()
        self.cb_export.currentIndexChanged.connect(self._sync_export_ui)
        self.sw_table.checkedChanged.connect(self._sync_target_summary)
        self.sw_image.checkedChanged.connect(self._sync_target_summary)
        self.file_card.files_changed.connect(self._sync_target_summary)
        self._on_batch_changed(False)  # 初始化显示状态
        self._sync_export_ui()

    def _on_batch_changed(self, checked):
        """批量模式切换时更新UI。"""
        self.result_card.setVisible(not checked)
        self._sync_target_summary()

    def _sync_export_ui(self, *_args):
        word_mode = self.cb_export.currentIndex() == 1
        self.sw_table.setEnabled(word_mode)
        self.sw_image.setEnabled(word_mode)
        self._sync_target_summary()

    def _sync_target_summary(self, *_args):
        fmt = "DOCX" if self.cb_export.currentIndex() == 1 else "TXT"
        prefix = tr("批量 OCR", "Batch OCR") if self.sw_batch.isChecked() else tr("OCR", "OCR")
        self.file_card.set_target_fmt(f"{prefix} / {fmt}")
        self.action_bar.btn_go.setText(
            tr("开始批量识别", "Run batch OCR")
            if self.sw_batch.isChecked() else tr("开始识别", "Run OCR"))
        if self.cb_export.currentIndex() == 0:
            self.mode_hint.setText(tr(
                "TXT 会逐页识别并输出纯文字，不保留原始版式、图片或可编辑表格。PDF 各页文字之间以空行分隔。",
                "TXT recognizes each page as plain text. It does not preserve layout, images, or editable tables; PDF pages are separated by blank lines."))
        else:
            details = []
            if self.sw_table.isChecked():
                details.append(tr("尝试重建可编辑表格", "attempt editable table reconstruction"))
            if self.sw_image.isChecked():
                details.append(tr("嵌入扫描页原图", "embed scanned page images"))
            option_text = (tr("；当前会{}。", "; currently set to {}.").format(
                tr("并", " and ").join(details)) if details else
                tr("；当前只输出识别文字。", "; currently outputs recognized text only."))
            self.mode_hint.setText(tr(
                "Word 会自动判断 PDF 类型：数字 PDF 优先保留已有文字和版式，扫描件与图片使用 OCR 重建内容{}识别结果和表格结构仍建议人工复核。",
                "Word detects the PDF type automatically: digital PDFs prioritize existing text and layout, while scans and images rebuild content with OCR{} Review recognized text and table structure before use.").format(option_text))

        files = self.file_card.files()
        count = len(files)
        ext = ".docx" if self.cb_export.currentIndex() == 1 else ".txt"
        if self.sw_batch.isChecked():
            mode_text = tr(
                "每个输入各生成 1 个同名 {ext} 文件，共 {count} 个结果。",
                "Each input creates one matching {ext} file, for {count} results.").format(
                    ext=ext, count=count)
        elif count > 1:
            mode_text = tr(
                "已添加 {count} 个文件；单文件模式只能保留 1 个，或开启批量模式。",
                "{count} files added. Keep one file for single mode, or enable batch mode.").format(count=count)
        else:
            mode_text = tr(
                "单文件模式会生成 1 个同名 {ext} 文件，并在下方显示可提取文字。",
                "Single mode creates one matching {ext} file and previews extractable text below.").format(ext=ext)
        self.output_hint.setText(mode_text + tr(
            " 重名处理沿用全局设置，源文件保持不变。",
            " Name conflicts follow global settings; source files stay unchanged."))

    # ── 结果操作 ─────────────────────────────────
    def _copy_result(self):
        text = self.txt_result.toPlainText().strip()
        if text:
            QGuiApplication.clipboard().setText(text)
            toast.show_success(self, tr("已复制到剪贴板", "Copied to clipboard"))

    def _export_txt(self):
        text = self.txt_result.toPlainText().strip()
        if not text:
            toast.show_warning(self, tr("暂无识别结果", "No recognized text yet"))
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, tr("导出识别结果", "Export result"), "ocr_result.txt",
            tr("文本文件 (*.txt)", "Text files (*.txt)"))
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                toast.show_success(self, tr("已导出至：{}", "Exported to: {}")
                                   .format(os.path.basename(path)))
            except OSError as e:
                toast.show_error(self, tr("导出失败：{}", "Export failed: {}")
                                 .format(e))

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "lang": OCR_LANGS[0],
            "export_fmt": "docx" if self.cb_export.currentIndex() == 1 else "txt",
            "batch_mode": self.sw_batch.isChecked(),
            "keep_images": self.sw_image.isChecked(),
            "table_recognition": self.sw_table.isChecked(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("lang") in _LEGACY_LANGS | set(OCR_LANGS):
            self.cb_lang.setCurrentIndex(0)
        self.cb_export.setCurrentIndex(
            1 if prefs.get("export_fmt") == "docx" else 0)
        if "batch_mode" in prefs:
            self.sw_batch.setChecked(bool(prefs["batch_mode"]))
        if "keep_images" in prefs:
            self.sw_image.setChecked(bool(prefs["keep_images"]))
        if "table_recognition" in prefs:
            self.sw_table.setChecked(bool(prefs["table_recognition"]))
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core import ocr_batch
        params = task.params or {}
        if params.get("export_fmt") == "docx":
            return ocr_batch.ocr_file_to_docx(
                task.file_path, task.output_path,
                lang=params.get("lang", "chi_sim+eng"),
                keep_images=params.get("keep_images", True),
                table_recognition=params.get("table_recognition", True),
                progress_cb=prog)
        return ocr_batch.ocr_file_to_txt(
            task.file_path, task.output_path,
            lang=params.get("lang", "chi_sim+eng"), progress_cb=prog)

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        ext = ".docx" if params["export_fmt"] == "docx" else ".txt"
        out_path = tm.make_output_path(f, out_dir, ext)
        base, output_ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{output_ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(
            name=f"{tr('OCR识别', 'OCR')} - {os.path.basename(f)}",
            task_type="ocr", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="ocr_batch",
            history_type=tr("OCR 识别", "OCR"), history_target=params["lang"],
            need_ffmpeg=False)

    def _start(self):
        files = self.file_card.files()
        if not self.sw_batch.isChecked() and len(files) > 1:
            toast.show_warning(
                self, tr("单文件模式只能识别一个文件；请移除多余文件或开启批量模式",
                         "Single-file mode accepts one file; remove extras or enable batch mode"))
            return False
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        if not self.sw_batch.isChecked():
            self.txt_result.clear()
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要识别的图片或PDF", "Add images or PDFs to recognize first")

    # ── 状态联动：成功后回填结果文本区 ──────────
    def _on_state(self, task_id, state):
        task = self.services.task_manager.get_task(task_id)
        if (task and task.task_type == "ocr" and state == tm.SUCCESS
                and os.path.isfile(task.output_path)
                and not task.params.get("batch_mode", False)):
            try:
                if task.params.get("export_fmt") == "docx":
                    from core.ocr_batch import extract_docx_text
                    text = extract_docx_text(task.output_path)
                else:
                    with open(task.output_path, "r", encoding="utf-8") as f:
                        text = f.read()
                if text.strip():
                    self.txt_result.setPlainText(text)
                else:
                    toast.show_warning(
                        self, tr("识别已完成，但结果中没有可预览文字",
                                 "OCR finished, but no previewable text was found"))
            except (OSError, UnicodeError) as exc:
                toast.show_error(
                    self, tr("结果读取失败：{}", "Could not read result: {}").format(exc))
        super()._on_state(task_id, state)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "settings_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
