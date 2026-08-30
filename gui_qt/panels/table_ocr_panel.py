"""table_ocr_panel — 表格识别面板（图片 → CSV / Excel）。

基于 core.table_recognizer（RapidOCR 文字 + 位置聚类成表）。
"""
import os

from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
OUTPUT_FORMATS = ("csv", "xlsx")
CHART_TYPES = (None, "bar", "line", "pie")


class TableOcrPanelPage(BaseQtPanel, TaskPanelMixin):
    """表格识别页。"""

    panel_key = "table_ocr"
    need_ffmpeg = False

    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("表格识别", "Table OCR"),
            tr("识别图片中的规则表格并输出为 CSV 或 Excel；复杂合并单元格可能需要人工校正",
               "Recognize regular image tables as CSV or Excel; complex merged cells may need manual correction"),
            FluentIcon.TILES)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=IMAGE_EXTS)
        lay.addWidget(self.file_card)
        self.input_hint = CaptionLabel(tr(
            "建议使用画面端正、文字清晰、行列间距稳定的表格图片。当前仅支持图片，不支持直接添加 PDF。",
            "Use a straight, clear table image with consistent rows and columns. This tool accepts images only, not PDF files."))
        self.input_hint.setWordWrap(True)
        lay.addWidget(self.input_hint)

        lay.addWidget(self._build_params_card())

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始识别", "OCR"))
        lay.addWidget(self.action_bar)

        from core.table_recognizer import make_runner
        self.services.task_manager.register_runner("table_ocr", make_runner)
        self._reserved_output_paths = set()
        self._wire_tasks()
        self.cb_chart.currentIndexChanged.connect(self._sync_format_ui)
        self.file_card.files_changed.connect(self._sync_format_ui)
        self._sync_format_ui()

    def _build_params_card(self):
        sec = FormSection(tr("识别设置", "OCR Settings"), FluentIcon.SETTING)
        self.settings_grid = FormGrid(columns=2)
        self.cb_fmt = ComboBox()
        self.cb_fmt.addItems(["CSV", "Excel (XLSX)"])
        self.cb_fmt.setCurrentIndex(0)
        self.cb_fmt.currentIndexChanged.connect(self._sync_format_ui)
        self.settings_grid.add_field(
            tr("输出格式", "Output format"), self.cb_fmt,
            hint=tr("CSV 通用轻量；Excel 支持工作表和可选图表",
                    "CSV is lightweight; Excel supports worksheets and optional charts"))
        self.cb_chart = ComboBox()
        self.cb_chart.addItems([tr("不生成图表", "No chart"),
                                tr("柱状图", "Bar chart"),
                                tr("折线图", "Line chart"),
                                tr("饼图", "Pie chart")])
        self.cb_chart.setCurrentIndex(0)
        self.cb_chart.setEnabled(False)
        self.settings_grid.add_field(
            tr("生成图表（仅 Excel）", "Chart (Excel only)"), self.cb_chart,
            hint=tr("首行为表头、首列为分类、其余列应为数值",
                    "Row 1 is the header, column 1 the categories, and remaining columns should be numeric"))
        sec.add_form(self.settings_grid)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)
        return sec

    def _sync_format_ui(self, *_args):
        """图表仅 Excel 模式可用，并同步文件规格与主操作。"""
        is_excel = self.cb_fmt.currentIndex() == 1
        self.cb_chart.setEnabled(is_excel)
        fmt = "XLSX" if is_excel else "CSV"
        self.file_card.set_target_fmt(f"{tr('表格识别', 'Table OCR')} / {fmt}")
        self.action_bar.btn_go.setText(
            tr("识别并导出 Excel", "OCR to Excel")
            if is_excel else tr("识别并导出 CSV", "OCR to CSV"))
        if not is_excel:
            self.mode_hint.setText(tr(
                "CSV 只保存识别出的行列文字，不保留图片样式、边框或合并单元格；文件使用 UTF-8 编码，可直接用常见表格软件打开。",
                "CSV stores recognized rows and cells only. It does not preserve image styling, borders, or merged cells, and uses UTF-8 for broad compatibility."))
        elif self.cb_chart.currentIndex() == 0:
            self.mode_hint.setText(tr(
                "Excel 会生成一个“表格”工作表。识别结果按行列写入，但不会还原原图样式、边框或复杂合并单元格。",
                "Excel creates one Table worksheet. Recognized cells are written by row and column, without recreating source styling, borders, or complex merged cells."))
        else:
            self.mode_hint.setText(tr(
                "图表使用首行作为系列名称、首列作为分类，其余列需为数值。数据不足或格式不适合时会跳过图表，但仍保留识别出的表格数据。",
                "Charts use row 1 as series names, column 1 as categories, and numeric values in remaining columns. If data is unsuitable, the chart is skipped while table data is kept."))
        count = len(self.file_card.files())
        ext = ".xlsx" if is_excel else ".csv"
        chart_text = (tr("，并尝试生成{}", ", with chart type: {}").format(
            self.cb_chart.currentText())
                      if is_excel and self.cb_chart.currentIndex() > 0 else "")
        self.output_hint.setText(tr(
            "整批 {count} 张图片，每张生成 1 个同名 {ext} 文件{chart}。重名处理沿用全局设置，源图片保持不变。",
            "Batch: {count} images. Each creates one matching {ext} file{chart}. Name conflicts follow global settings; source images stay unchanged.").format(
                count=count, ext=ext, chart=chart_text))

    def _start(self):
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self) -> str:
        return tr("请先添加要识别的图片", "Add images to recognize first")

    def collect_params(self) -> dict:
        """将界面选择转换为不依赖语言文案的稳定任务参数。"""
        output_format = OUTPUT_FORMATS[
            self.cb_fmt.currentIndex() if self.cb_fmt.currentIndex() in (0, 1) else 0]
        chart_type = CHART_TYPES[
            self.cb_chart.currentIndex()
            if 0 <= self.cb_chart.currentIndex() < len(CHART_TYPES) else 0]
        if output_format != "xlsx":
            chart_type = None
        return {"output_format": output_format, "chart_type": chart_type}

    def _make_task(self, f: str) -> dict:
        params = self.collect_params()
        ext = f".{params['output_format']}"
        out = tm.make_output_path(f, self.out_row.resolve_dir(f), ext)
        base, output_ext = os.path.splitext(out)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out))
        while normalized in self._reserved_output_paths:
            out = f"{base}_{counter}{output_ext}"
            normalized = os.path.normcase(os.path.abspath(out))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(name=f"{tr('表格识别', 'Table OCR')} - {os.path.basename(f)}",
                    task_type="table_ocr",
                    file_path=f, output_path=out, params=params,
                    runner=self._runner, runner_key="table_ocr",
                    history_type=tr("表格识别", "Table OCR"), history_target=ext[1:].upper(),
                    need_ffmpeg=False)

    def _runner(self, task, prog):
        from core import table_recognizer
        return table_recognizer.recognize_table(
            task.file_path, task.output_path, progress_cb=prog,
            chart_type=task.params.get("chart_type"))

    def collect_prefs(self) -> dict:
        """记忆输出格式/图表选择，重进面板自动恢复。"""
        return {
            "output_format": OUTPUT_FORMATS[self.cb_fmt.currentIndex()],
            "chart_type": CHART_TYPES[self.cb_chart.currentIndex()],
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        output_format = prefs.get("output_format")
        if output_format in OUTPUT_FORMATS:
            self.cb_fmt.setCurrentIndex(OUTPUT_FORMATS.index(output_format))
        elif isinstance(prefs.get("fmt"), int):
            self.cb_fmt.setCurrentIndex(max(0, min(prefs["fmt"], 1)))
        chart_type = prefs.get("chart_type")
        if "chart_type" in prefs and chart_type in CHART_TYPES:
            self.cb_chart.setCurrentIndex(CHART_TYPES.index(chart_type))
        elif isinstance(prefs.get("chart"), int):
            self.cb_chart.setCurrentIndex(
                max(0, min(prefs["chart"], self.cb_chart.count() - 1)))
        self._sync_format_ui()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "settings_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
