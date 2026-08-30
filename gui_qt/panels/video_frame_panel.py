# -*- coding: utf-8 -*-
"""video_frame_panel — 视频抽帧 / 缩略图墙面板（合并版）。

两种输出模式（SegmentedWidget 切换）：
- 单张序列：按固定时间间隔批量截取关键帧（extract_frames，FFmpeg fps 滤镜）
- 缩略图墙：提取多帧生成 N×M 网格图（generate_thumbnail_sheet）
"""
import os

from PySide6.QtWidgets import QHBoxLayout, QSizePolicy
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, PushButton, SegmentedWidget)

from gui_qt.i18n import tr
from gui_qt import task_manager as tm
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".wmv", ".mov", ".flv", ".webm", ".ts",
              ".m4v", ".mpg", ".mpeg", ".3gp"}

INTERVAL_VALUES = ["0.5", "1", "2", "3", "5", "10", "30", "60"]
FORMAT_VALUES = ["PNG", "JPG"]
COLS_VALUES = ["2", "3", "4", "5", "6", "8"]
ROWS_VALUES = ["2", "3", "4", "5", "6", "8"]
WIDTH_VALUES = ["800", "1200", "1600", "2000", "2400"]
MODE_FRAMES = "frames"
MODE_SHEET = "sheet"


class VideoFramePanelPage(BaseQtPanel, TaskPanelMixin):
    """视频抽帧 / 缩略图墙页。"""

    panel_key = "frame_extract"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频抽帧 / 缩略图", "Extract frames / Thumbnails"),
            tr("按间隔批量截取画面，或为每个视频生成一张缩略图墙",
               "Capture frames at intervals, or create one thumbnail sheet per video"),
            FluentIcon.CAMERA))

        self.file_card = FileListCard(tr("视频列表", "Video list"), file_exts=VIDEO_EXTS)
        lay.addWidget(self.file_card)

        # 取景器属于当前文件上下文：预览选中项，未选择时回退第一项；
        # 空列表时禁用，避免用户点击后才得到可预防的错误提示。
        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        self.btn_picker = PushButton(
            FluentIcon.CAMERA, tr("打开取景器", "Open Frame Picker"))
        self.btn_picker.clicked.connect(self._open_picker)
        picker_row.addWidget(self.btn_picker)
        self.picker_hint = CaptionLabel()
        self.picker_hint.setWordWrap(True)
        self.picker_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        picker_row.addWidget(self.picker_hint, 1)
        self.file_card.layout().addLayout(picker_row)

        # 输出模式：单张序列 / 缩略图墙
        self.sg_mode = SegmentedWidget(self)
        self.sg_mode.addItem(MODE_FRAMES, tr("单张序列", "Frame sequence"))
        self.sg_mode.addItem(MODE_SHEET, tr("缩略图墙", "Thumbnail sheet"))
        self.sg_mode.setCurrentItem(MODE_FRAMES)
        self.sg_mode.setAccessibleName(tr("输出模式", "Output mode"))
        self.sg_mode.currentItemChanged.connect(self._on_mode_changed)
        self.sg_mode.setFixedHeight(32)
        lay.addWidget(self.sg_mode)
        self._mode = MODE_FRAMES

        from gui_qt.components.form_widgets import FormGrid, FormSection

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        # 单张序列设置
        self.card_frames = FormSection(tr("抽帧设置", "Frame settings"), FluentIcon.CAMERA)
        self.frames_grid = FormGrid(columns=2)
        self.cb_interval = self.frames_grid.add_field(
            tr("间隔（秒）", "Interval (sec)"), _combo(INTERVAL_VALUES, "1"),
            hint=tr("每隔多少秒截取一帧", "Capture one frame every N seconds"))
        self.cb_fmt = self.frames_grid.add_field(
            tr("输出格式", "Output format"), _combo(FORMAT_VALUES, "PNG"),
            hint=tr("JPG 体积更小，PNG 画质无损", "JPG smaller, PNG lossless"))
        self.card_frames.add_form(self.frames_grid)
        self.frames_hint = CaptionLabel()
        self.frames_hint.setWordWrap(True)
        self.card_frames.add_widget(self.frames_hint)
        lay.addWidget(self.card_frames)

        # 缩略图墙设置（默认隐藏）
        self.card_sheet = FormSection(tr("缩略图布局", "Thumbnail layout"), FluentIcon.LAYOUT)
        self.sheet_grid = FormGrid(columns=3)
        self.cb_cols = self.sheet_grid.add_field(
            tr("列数", "Columns"), _combo(COLS_VALUES, "4"),
            hint=tr("缩略图网格的列数", "Thumbnail grid columns"))
        self.cb_rows = self.sheet_grid.add_field(
            tr("行数", "Rows"), _combo(ROWS_VALUES, "4"),
            hint=tr("缩略图网格的行数", "Thumbnail grid rows"))
        self.cb_width = self.sheet_grid.add_field(
            tr("整图宽度（px）", "Sheet width (px)"), _combo(WIDTH_VALUES, "1600"),
            hint=tr("输出图片的宽度（像素）", "Output image width (px)"))
        self.card_sheet.add_form(self.sheet_grid)
        self.sheet_hint = CaptionLabel()
        self.sheet_hint.setWordWrap(True)
        self.card_sheet.add_widget(self.sheet_hint)
        lay.addWidget(self.card_sheet)
        self.card_sheet.setVisible(False)

        from gui_qt.components.form_widgets import FormSection
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始生成", "Generate"))
        lay.addWidget(self.action_bar)

        # 注册 runner 工厂：持久化恢复的任务可在此面板上下文重建执行器
        self.services.task_manager.register_runner(
            "frame_extract", lambda task: self._runner)
        self._wire_tasks()
        self.file_card.files_changed.connect(self._sync_picker_enabled)
        self.file_card.files_changed.connect(self._sync_target_summary)
        self.file_card.table.itemSelectionChanged.connect(self._sync_picker_enabled)
        self.file_card.file_double_clicked.connect(self._open_picker)
        self.cb_interval.currentTextChanged.connect(self._sync_target_summary)
        self.cb_fmt.currentTextChanged.connect(self._sync_target_summary)
        self.cb_cols.currentTextChanged.connect(self._sync_target_summary)
        self.cb_rows.currentTextChanged.connect(self._sync_target_summary)
        self.cb_width.currentTextChanged.connect(self._sync_target_summary)
        self._reserved_frame_dirs = set()
        self._sync_picker_enabled()
        self._sync_target_summary()

    def _on_mode_changed(self, key):
        if key not in (MODE_FRAMES, MODE_SHEET):
            return
        self._mode = key
        self.card_frames.setVisible(key == MODE_FRAMES)
        self.card_sheet.setVisible(key == MODE_SHEET)
        self._sync_target_summary()

    def _sync_target_summary(self):
        """文件列表同步展示当前输出规格，避免提交前无法确认结果类型。"""
        interval = float(self.cb_interval.currentText())
        self.frames_hint.setText(tr(
            "每 {interval} 秒抽取 1 张，每分钟约 {density} 张；实际数量取决于视频时长和帧率。间隔越小，图片越多。",
            "One frame every {interval} sec, about {density} per minute; actual count depends on duration and frame rate. Shorter intervals produce more images.").format(
                interval=self.cb_interval.currentText(), density=f"{60 / interval:g}"))
        cols, rows = int(self.cb_cols.currentText()), int(self.cb_rows.currentText())
        requested_width = int(self.cb_width.currentText())
        # 引擎将整图宽度整除到每一列；展示真实对齐后的宽度，不把设定值当精确输出。
        cell_width = requested_width // cols
        self.sheet_hint.setText(tr(
            "{cols} 列 × {rows} 行，共 {count} 个画面，沿视频时长均匀取样。每格宽 {cell}px，整图宽 {width}px；高度随视频比例确定，宽度按列数对齐。",
            "{cols} columns × {rows} rows: {count} frames sampled evenly across the video. Each cell is {cell}px wide; sheet width is {width}px. Height follows the video aspect ratio; width aligns to columns.").format(
                cols=cols, rows=rows, count=cols * rows, cell=cell_width, width=cell_width * cols))
        count = len(self.file_card.files())
        if self._mode == MODE_SHEET:
            summary = f"PNG · {cols}×{rows} · {cell_width * cols} px"
            self.action_bar.btn_go.setText(tr("生成缩略图墙", "Generate sheets"))
            self.output_hint.setText(tr(
                "整批 {count} 个视频，各生成 1 张 PNG，命名示例：视频名_thumbnails.png。重名沿用全局冲突设置；取景器手动导出独立于本批设置。",
                "Each of the {count} videos produces one PNG, e.g. video_thumbnails.png. Name conflicts follow global settings; manual frame-picker exports are separate.").format(count=count))
        else:
            summary = tr("{} · 每 {} 秒", "{} · Every {} sec").format(
                self.cb_fmt.currentText(), self.cb_interval.currentText())
            self.action_bar.btn_go.setText(tr("开始抽帧", "Extract frames"))
            self.output_hint.setText(tr(
                "整批 {count} 个视频，各存入独立目录（如“视频名_frames”），从 frame_00000.{ext} 起编号；重名沿用全局冲突设置。取景器用于手动选帧。",
                "Each of the {count} videos gets a separate folder (e.g. video_frames), numbered from frame_00000.{ext}; name conflicts follow global settings. The frame picker is for manual selection.").format(
                    count=count, ext=self.cb_fmt.currentText().lower()))
        self.file_card.set_target_fmt(summary)

    # ── 取景器 ──────────────────────────────────
    def _selected_file(self):
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        files = self.file_card.files()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _sync_picker_enabled(self):
        selected = self._selected_file()
        enabled = bool(selected)
        self.btn_picker.setEnabled(enabled)
        self.btn_picker.setToolTip(
            tr("预览选中视频、拖动时间轴并导出当前帧",
               "Preview the selected video, seek, and export a frame")
            if enabled else tr("请先添加视频文件", "Add a video first"))
        self.picker_hint.setText(
            tr("手动选帧：{}", "Manual frame selection: {}").format(os.path.basename(selected))
            if selected else tr("添加视频后可手动选帧；批量生成使用下方设置。", "Add a video for manual frame selection; batch generation uses the settings below."))
        self.picker_hint.setToolTip(selected)

    def _open_picker(self, file_path=""):
        """打开独立取景器（手动选帧导出）。"""
        selected = file_path or self._selected_file()
        if not selected:
            from gui_qt.components import toast
            toast.show_warning(self, tr("请先添加要抽帧的视频",
                                        "Add a video first"))
            return
        # 取景器会直接写入当前输出目录，与批量入口一致地拦截未填完的自定义目录。
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            from gui_qt.components import toast
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return
        from gui_qt.components.frame_picker import FramePickerDialog
        dlg = FramePickerDialog(
            selected, out_dir=self.out_row.resolve_dir(selected), parent=self)
        dlg.exec()

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "mode": self._mode,
            "interval": self.cb_interval.currentText(),
            "fmt": self.cb_fmt.currentText(),
            "cols": self.cb_cols.currentText(),
            "rows": self.cb_rows.currentText(),
            "width": self.cb_width.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("interval") in INTERVAL_VALUES:
            self.cb_interval.setCurrentText(str(prefs["interval"]))
        if prefs.get("fmt") in FORMAT_VALUES:
            self.cb_fmt.setCurrentText(str(prefs["fmt"]))
        if prefs.get("cols") in COLS_VALUES:
            self.cb_cols.setCurrentText(str(prefs["cols"]))
        if prefs.get("rows") in ROWS_VALUES:
            self.cb_rows.setCurrentText(str(prefs["rows"]))
        if prefs.get("width") in WIDTH_VALUES:
            self.cb_width.setCurrentText(str(prefs["width"]))
        if prefs.get("mode") in ("frames", "sheet"):
            self.sg_mode.setCurrentItem(prefs["mode"])
            self._on_mode_changed(prefs["mode"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        if p.get("mode") == "sheet":
            from core.thumbnail_sheet import generate_thumbnail_sheet
            return generate_thumbnail_sheet(
                task.file_path, task.output_path,
                int(p.get("cols", 4)), int(p.get("rows", 4)),
                int(p.get("width", 1600)), prog)
        from core.video_frame_extract import extract_frames
        out_dir = os.path.dirname(task.output_path)
        ok, _n = extract_frames(
            task.file_path, out_dir,
            float(p.get("interval", 1) or 1), p.get("fmt", "PNG"), prog)
        return ok

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        if params["mode"] == "sheet":
            nm = os.path.splitext(os.path.basename(f))[0]
            # 统一走 make_output_path：应用冲突策略 + 源目同路径保护
            out_path = tm.make_output_path(f, out_dir, ".png",
                                           name=nm + "_thumbnails")
            return dict(
                name=f"{tr('视频缩略图', 'Video thumbnails')} - {os.path.basename(f)}",
                task_type="frame_extract", file_path=f,
                output_path=out_path, params=params,
                runner=self._runner, runner_key="frame_extract",
                history_type=tr("视频缩略图", "Video thumbnails"),
                history_target=f"{params['cols']}×{params['rows']}",
                need_ffmpeg=True)
        ext = ".jpg" if params["fmt"] == "JPG" else ".png"
        frames_dir = tm.make_output_dir(f, out_dir, "_frames")
        # 两个不同目录中的同名视频在同一批提交时，首个目录尚未创建；
        # 仍需在内存中去重，避免并行任务落到同一个结果目录。
        base_dir = frames_dir
        counter = 1
        normalized = os.path.normcase(os.path.abspath(frames_dir))
        while normalized in self._reserved_frame_dirs:
            frames_dir = f"{base_dir}_{counter}"
            normalized = os.path.normcase(os.path.abspath(frames_dir))
            counter += 1
        self._reserved_frame_dirs.add(normalized)
        return dict(
            name=f"{tr('视频抽帧', 'Extract frames')} - {os.path.basename(f)}",
            task_type="frame_extract", file_path=f,
            output_path=os.path.join(frames_dir, "frame_00000" + ext),
            params=params, runner=self._runner, runner_key="frame_extract",
            history_type=tr("视频抽帧", "Extract frames"),
            history_target=tr("每 {} 秒 · {}", "Every {} sec · {}").format(
                params["interval"], params["fmt"]),
            need_ffmpeg=True)

    def _start(self):
        self._reserved_frame_dirs = {
            os.path.normcase(os.path.abspath(os.path.dirname(task.output_path)))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
            and task.params.get("mode") == "frames"
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要抽帧的视频文件", "Add videos to extract frames first")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.viewport().width()
        frames_grid = getattr(self, "frames_grid", None)
        sheet_grid = getattr(self, "sheet_grid", None)
        if frames_grid is not None:
            frames_grid.set_columns(1 if width < 820 else 2)
        if sheet_grid is not None:
            sheet_grid.set_columns(1 if width < 820 else 3)
