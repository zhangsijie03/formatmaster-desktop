"""gif_panel — 视频转 GIF 面板（阶段2 迁移自 gui/panels/gif_panel.py）。

将视频片段转换为 GIF 动图，支持自定义宽度/帧率/起始时间/时长。
FFmpeg 命令与 tkinter 版 _run_task_general 的 gif 分支一致：
-vf fps=..,scale=..:flags=lanczos -loop 0，-progress pipe:1 解析进度。
"""
import os
from qfluentwidgets import (CaptionLabel, CheckBox, ComboBox, DoubleSpinBox,
                            FluentIcon)

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

# 预置值（与 tkinter 版 gif_panel 一致）
WIDTH_VALUES = [tr("原始", "Original"), "640", "480", "320", "240"]
FPS_VALUES = ["10", "15", "20", "24", "30"]
GIF_SRC_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".flv", ".webm", ".ts"}


class GifPanelPage(BaseQtPanel, TaskPanelMixin):
    """视频转 GIF 页。"""

    panel_key = "gif"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频转 GIF", "Video to GIF"),
            tr("选择视频片段，再调整动图尺寸和流畅度",
               "Choose a video clip, then adjust GIF size and smoothness"),
            FluentIcon.MOVIE))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=GIF_SRC_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt("GIF")

        from gui_qt.components.form_widgets import FormSection, FormGrid
        card = FormSection(tr("GIF设置", "GIF settings"), FluentIcon.MOVIE)
        self.params_grid = FormGrid(columns=2)

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        self.cb_w = self.params_grid.add_field(
            tr("宽度（px）", "Width (px)"), _combo(WIDTH_VALUES, "480"),
            hint=tr("输出 GIF 宽度，原始保持原尺寸", "Output GIF width, original = keep size"))
        self.cb_fps = self.params_grid.add_field(
            tr("帧率（fps）", "Frame rate (fps)"), _combo(FPS_VALUES, "15"),
            hint=tr("帧率越高动图越流畅，文件也越大", "Higher FPS = smoother GIF, larger file"))
        self.sb_start = DoubleSpinBox()
        self.sb_start.setRange(0.0, 86400.0)
        self.sb_start.setDecimals(1)
        self.sb_start.setSingleStep(0.5)
        self.sb_start.setSuffix(" s")
        self.params_grid.add_field(
            tr("开始时间", "Start time"), self.sb_start,
            hint=tr("从视频的指定时间开始截取",
                    "Start extracting at this timestamp"))
        self.sb_dur = DoubleSpinBox()
        self.sb_dur.setRange(0.2, 3600.0)
        self.sb_dur.setDecimals(1)
        self.sb_dur.setSingleStep(0.5)
        self.sb_dur.setValue(10.0)
        self.sb_dur.setSuffix(" s")
        self.params_grid.add_field(
            tr("片段时长", "Clip duration"), self.sb_dur,
            hint=tr("生成 GIF 的片段时长",
                    "Duration of the generated GIF clip"))
        card.add_form(self.params_grid)

        self.cb_all = CheckBox(tr("从开始时间转换到视频结尾",
                                  "Convert from start time to the end"))
        card.add_widget(self.cb_all)
        self.range_summary = CaptionLabel()
        self.range_summary.setWordWrap(True)
        card.add_widget(self.range_summary)
        self.cb_all.toggled.connect(self._sync_range_summary)
        self.sb_start.valueChanged.connect(self._sync_range_summary)
        self.sb_dur.valueChanged.connect(self._sync_range_summary)
        self._sync_range_summary()

        # 胶片视图：预览 + 胶片条选时间段，一键回填开始/时长
        from PySide6.QtWidgets import QHBoxLayout, QWidget
        from qfluentwidgets import PushButton
        film_row = QWidget()
        fr = QHBoxLayout(film_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(8)
        self.btn_film = PushButton(
            FluentIcon.PHOTO, tr("打开胶片视图", "Open Filmstrip"))
        self.btn_film.setToolTip(
            tr("预览视频并在胶片条上拖动游标，精确选择要转 GIF 的时间段",
               "Preview video, drag in/out handles on the filmstrip to pick a range"))
        self.btn_film.clicked.connect(self._open_filmstrip)
        fr.addWidget(self.btn_film)
        self.film_source_label = CaptionLabel()
        self.film_source_label.setWordWrap(True)
        fr.addWidget(self.film_source_label, 1)
        card.add_widget(film_row)
        lay.addWidget(card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始转换", "Convert"))
        lay.addWidget(self.action_bar)

        self._wire_tasks()
        self.file_card.files_changed.connect(self._sync_film_source)
        self.file_card.table.itemSelectionChanged.connect(self._sync_film_source)
        self._sync_film_source()

    def _sync_range_summary(self, *_args) -> None:
        """明确展示实际提交的时间模式，避免将禁用的记忆时长误认作生效值。"""
        to_end = self.cb_all.isChecked()
        self.sb_dur.setEnabled(not to_end)
        start = self.sb_start.value()
        if to_end:
            text = tr("设定区间：从 {:.1f} 秒到各视频结尾。",
                      "Requested range: from {:.1f}s to the end of each video.").format(start)
        else:
            duration = self.sb_dur.value()
            text = tr("设定区间：{:.1f}–{:.1f} 秒，时长 {:.1f} 秒。",
                      "Requested range: {:.1f}–{:.1f}s, duration {:.1f}s.").format(
                          start, start + duration, duration)
        self.range_summary.setText(text + tr(
            "同一设置应用于全部文件，每个视频分别生成 GIF。",
            " The same settings apply to all files; each video produces its own GIF."))

    def _selected_film_file(self) -> str:
        """胶片视图优先使用选中行，未选择时仍沿用第一份视频。"""
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        if rows and 0 <= rows[0].row() < len(files):
            return files[rows[0].row()]
        return files[0] if files else ""

    def _sync_film_source(self) -> None:
        path = self._selected_film_file()
        self.btn_film.setEnabled(bool(path))
        self.film_source_label.setText(
            os.path.basename(path) if path else
            tr("添加视频后可预览选段", "Add a video to preview and select a clip"))
        self.film_source_label.setToolTip(path)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "width": self.cb_w.currentText(),
            "fps": self.cb_fps.currentText(),
            "start": self.sb_start.value(),
            "duration": None if self.cb_all.isChecked() else self.sb_dur.value(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "width": self.cb_w.currentText(),
            "fps": self.cb_fps.currentText(),
            "start": self.sb_start.value(),
            "duration": self.sb_dur.value(),
            "all_duration": self.cb_all.isChecked(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("width") in WIDTH_VALUES:
            self.cb_w.setCurrentText(prefs["width"])
        if prefs.get("fps") in FPS_VALUES:
            self.cb_fps.setCurrentText(prefs["fps"])
        try:
            self.sb_start.setValue(max(0.0, float(prefs.get("start", 0))))
        except (TypeError, ValueError):
            self.sb_start.setValue(0.0)
        duration = prefs.get("duration", 10)
        if duration not in (tr("全部", "All"), "All", "全部", None):
            try:
                self.sb_dur.setValue(max(0.2, float(duration)))
            except (TypeError, ValueError):
                self.sb_dur.setValue(10.0)
        self.cb_all.setChecked(
            bool(prefs.get("all_duration", False))
            or duration in (tr("全部", "All"), "All", "全部"))
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 胶片视图 ─────────────────────────────────
    def _open_filmstrip(self):
        """打开胶片视图，选定区间后回填开始/时长。"""
        files = self.file_card.files()
        if not files:
            from gui_qt.components import toast
            toast.show_info(self, tr("请先添加视频文件", "Add a video first"))
            return
        video = self._selected_film_file()
        from gui_qt.components.gif_filmstrip import GifFilmstripDialog
        dlg = GifFilmstripDialog(video, self)
        dlg.move(self.window().frameGeometry().center()
                 - dlg.rect().center())
        dlg.exec()
        rng = dlg.range_secs()
        if rng is None:
            return
        start, end = rng
        # 回填：开始取整秒（ffmpeg -ss 支持小数），时长 = end - start
        s_val = f"{int(start)}" if abs(start - int(start)) < 0.01 \
            else f"{start:.1f}"
        d_val = f"{end - start:.1f}"
        self.sb_start.setValue(float(s_val))
        self.sb_dur.setValue(float(d_val))
        self.cb_all.setChecked(False)
        from gui_qt.components import toast
        toast.show_success(
            self, tr("已应用区间：开始 {}s · 时长 {}s",
                     "Range applied: start {}s · duration {}s")
            .format(s_val, d_val))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core.video_tools import video_to_gif

        p = task.params
        width = p.get("width", tr("原始", "Original"))
        return video_to_gif(
            task.file_path, task.output_path,
            fps=p.get("fps", "15"),
            max_width=None if width in (tr("原始", "Original"),
                                         "Original", "原始") else width,
            start_sec=p.get("start", 0),
            duration_sec=p.get("duration"),
            progress_cb=prog,
            cancel_check=lambda: task.state == tm.CANCELLED)

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(f, out_dir, ".gif")
        return dict(
            name=f"{tr('视频转GIF', 'Video to GIF')} - {os.path.basename(f)}",
            task_type="gif", file_path=f, output_path=out_path,
            params=params, runner=self._runner,
            history_type=tr("视频转 GIF", "Video to GIF"), history_target="GIF")

    def _start(self):
        self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要转换的视频文件", "Add video files to convert first")

    def resizeEvent(self, event):
        """GIF 时间参数在窄窗口改为单列，保持数值可读可点。"""
        super().resizeEvent(event)
        if hasattr(self, "params_grid"):
            self.params_grid.set_columns(
                1 if self.viewport().width() < 820 else 2)
