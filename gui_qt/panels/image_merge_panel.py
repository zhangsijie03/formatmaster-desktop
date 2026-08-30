# -*- coding: utf-8 -*-
"""image_merge_panel — 图片拼接 / 合成 PDF 相册面板。

纵向/横向拼接多图成一张长图，或把多张图片一键合成 PDF 相册
（core.image_album，纯 PIL 实现）。
"""
import os

from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon

from gui_qt import task_manager as tm
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, ActionStatusState, FileListCard, OutputDirRow

ALBUM_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp", ".heic", ".heif"}
MODE_VALUES = [
    tr("纵向拼接（长图）", "Vertical (long image)"),
    tr("横向拼接", "Horizontal"),
    tr("合成 PDF（A4）", "PDF album (A4)"),
    tr("合成 PDF（原尺寸）", "PDF album (original size)"),
]
MODE_KEYS = ["vertical", "horizontal", "pdf_a4", "pdf_original"]
_MODE_ALIASES = dict(zip(MODE_VALUES, MODE_KEYS))
_MODE_ALIASES.update({
    "纵向拼接（长图）": "vertical", "Vertical (long image)": "vertical",
    "横向拼接": "horizontal", "Horizontal": "horizontal",
    "合成 PDF（A4）": "pdf_a4", "PDF album (A4)": "pdf_a4",
    "合成 PDF（原尺寸）": "pdf_original",
    "PDF album (original size)": "pdf_original",
})
GAP_VALUES = ["0", "10", "20", "40"]


def _mode_key(value):
    """兼容稳定 key 与旧翻译文案；损坏任务参数安全回退纵向拼接。"""
    try:
        mode = _MODE_ALIASES.get(value, value)
    except TypeError:
        return "vertical"
    return mode if mode in MODE_KEYS else "vertical"


class ImageMergePanelPage(BaseQtPanel, TaskPanelMixin):
    """图片拼接 / PDF 相册页。"""

    panel_key = "image_merge"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("图片拼接 / PDF 相册", "Image merge / PDF album"),
            tr("按拖拽顺序拼成长图，或一键合成 PDF 相册",
               "Merge images in drag order into a long image or PDF album"),
            FluentIcon.ALBUM)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("图片列表", "Images"), file_exts=ALBUM_EXTS)
        lay.addWidget(self.file_card)

        card = FormSection(tr("拼接设置", "Merge settings"), FluentIcon.ALBUM)
        self.settings_grid = FormGrid(columns=2)
        grid = self.settings_grid

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        self.cb_mode = grid.add_field(
            tr("模式", "Mode"), _combo(MODE_VALUES, MODE_VALUES[0]),
            hint=tr("拼图或合成 PDF 相册", "Merge or PDF album"))
        self.cb_gap = grid.add_field(
            tr("间距（px）", "Gap (px)"), _combo(GAP_VALUES, "10"),
            hint=tr("拼接时图片之间的空隙（仅拼图模式）", "Gap between images (merge modes only)"))
        card.add_form(grid)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        card.add_widget(self.mode_hint)
        lay.addWidget(card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)

        # 拼版预览：多行缩略图，可拖动图片换位（实时同步文件顺序）
        from gui_qt.components.visual_widgets import FrameGrid
        preview_card = FormSection(
            tr("拼接顺序", "Merge order"), FluentIcon.ALBUM)
        self.order_hint = CaptionLabel(
            tr("拖动缩略图调整顺序。长图按此顺序排列，PDF 按此顺序分页。",
               "Drag thumbnails to reorder. Long images follow this sequence; PDF pages use the same order."))
        self.order_hint.setProperty("sec", True)
        self.order_hint.setWordWrap(True)
        preview_card.add_widget(self.order_hint)
        self.merge_grid = FrameGrid()
        self.merge_grid.set_cell_size(120, 84)
        preview_card.add_widget(self.merge_grid)
        lay.addWidget(preview_card)
        # 输出目录放在顺序确认之后，避免最终顺序尚未确认就先处理保存位置。
        lay.addWidget(out_card)
        self.file_card.files_changed.connect(self._refresh_grid)
        self.file_card.files_changed.connect(self._sync_target_summary)
        self.merge_grid.order_changed.connect(self._on_grid_reorder)

        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)

        self.services.task_manager.register_runner(
            "image_merge", lambda task: self._runner)
        self._wire_tasks()
        self._merge_task_files = {}
        self.cb_mode.currentIndexChanged.connect(self._sync_mode_ui)
        self.cb_gap.currentTextChanged.connect(self._sync_target_summary)
        self._sync_mode_ui()

    def _refresh_grid(self):
        """文件变化 → 刷新拼版预览网格。"""
        self.merge_grid.set_images(self.file_card.files())

    def _on_grid_reorder(self, new_order):
        """拖拽换位 → 同步文件列表顺序（拼接按此顺序输出）。"""
        self.file_card.reorder(new_order)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        index = self.cb_mode.currentIndex()
        return {
            "mode": MODE_KEYS[index] if 0 <= index < len(MODE_KEYS) else "vertical",
            "gap": int(self.cb_gap.currentText()),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        mode = _mode_key(prefs.get("mode"))
        if mode in MODE_KEYS:
            self.cb_mode.setCurrentIndex(MODE_KEYS.index(mode))
        gap = str(prefs.get("gap", ""))
        if gap in GAP_VALUES:
            self.cb_gap.setCurrentText(gap)
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core.image_album import (merge_vertical, merge_horizontal, to_pdf)
        p = task.params
        raw_files = p.get("all_files")
        all_files = (list(raw_files)
                     if isinstance(raw_files, (list, tuple)) and raw_files
                     else [task.file_path])
        mode = _mode_key(p.get("mode"))
        if mode in ("pdf_a4", "pdf_original"):
            page_mode = "A4" if mode == "pdf_a4" else "original"
            return to_pdf(all_files, task.output_path, page_mode, prog)
        gap = int(p.get("gap", 10) or 0)
        if mode == "horizontal":
            return merge_horizontal(all_files, task.output_path, gap, prog)
        return merge_vertical(all_files, task.output_path, gap, prog)

    def _make_task(self, f):
        """单文件入口（持久化恢复用）：退化为单文件拼接。"""
        params = self.collect_params()
        params["all_files"] = [f]
        mode = params["mode"]
        out_dir = self.out_row.resolve_dir(f)
        if mode in ("pdf_a4", "pdf_original"):
            out_ext = ".pdf"
            hist_target = tr("PDF 相册", "PDF album")
        else:
            out_ext = ".jpg"
            hist_target = tr("拼图", "Merge")
        base = os.path.splitext(os.path.basename(f))[0]
        out_path = tm.make_output_path(
            f, out_dir, out_ext, name=base + "_merged")
        return dict(
            name=f"{tr('图片拼接', 'Image merge')} - {os.path.basename(f)}",
            task_type="image_merge", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="image_merge",
            history_type=tr("图片拼接", "Image merge"), history_target=hist_target,
            need_ffmpeg=False)

    def _submit_files(self):
        """多合一提交：整批文件作为一个任务（拼接/相册需要全部文件）。"""
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, self._empty_hint())
            return False
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return False
        active = [self.services.task_manager.get_task(task_id)
                  for task_id in self._task_rows]
        if any(task and task.state in (tm.WAITING, tm.RUNNING, tm.PAUSED)
               for task in active):
            self.action_bar.set_status(
                tr("当前拼接任务仍在处理中", "Merge task already processing"),
                ActionStatusState.WARNING)
            return True

        self.save_prefs()
        mgr = self.services.task_manager
        self._batch_results = []
        self._batch_progress = {}
        max_retries = int(self.services.get_pref("max_retries", 0) or 0)
        params = self.collect_params()
        params["all_files"] = list(files)
        mode = params["mode"]
        out_dir = self.out_row.resolve_dir(files[0])
        out_ext = ".pdf" if mode in ("pdf_a4", "pdf_original") else ".jpg"
        base = os.path.splitext(os.path.basename(files[0]))[0]
        out_name = base + "_merged"
        out_path = tm.make_output_path(
            files[0], out_dir, out_ext, name=out_name)
        kwargs = dict(
            name=f"{tr('图片拼接', 'Image merge')} - {len(files)} 张",
            task_type="image_merge", file_path=files[0], output_path=out_path,
            params=params, runner=self._runner, runner_key="image_merge",
            history_type=tr("图片拼接", "Image merge"),
            history_target=tr("{} 张", "{} images").format(len(files)),
            need_ffmpeg=False)
        kwargs.setdefault("max_retries", max_retries)
        tid = mgr.add_task(**kwargs)
        if tid is not None:
            self._task_rows[tid] = (files[0], self.file_card.row_of_file(files[0]))
            self._merge_task_files[tid] = list(files)
            self._batch_progress[tid] = 0
            self._set_inputs_enabled(False)
            self.action_bar.set_running(True)
            self.action_bar.set_status(
                tr("已提交任务（{} 张图片）", "Submitted ({} images)").format(len(files)))
            return True
        toast.show_error(self, tr("任务提交失败", "Submit failed"))
        return False

    def _start(self):
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要处理的图片文件", "Add images to process first")

    def _sync_mode_ui(self, *_args):
        mode = self.collect_params()["mode"]
        self.cb_gap.setEnabled(mode in ("vertical", "horizontal"))
        self._sync_target_summary()

    def _sync_target_summary(self, *_args):
        mode = self.collect_params()["mode"]
        if mode == "vertical":
            summary = tr("{} / 间距 {} px", "{} / {} px gap").format(
                self.cb_mode.currentText(), self.cb_gap.currentText())
            action = tr("开始拼接", "Merge images")
            self.mode_hint.setText(tr(
                "所有图片按顺序向下排列，并等比缩放到整批最大宽度，不会裁剪。间距和透明区域使用白色。",
                "Images are stacked downward and scaled proportionally to the batch's widest image without cropping. Gaps and transparent areas use white."))
        elif mode == "horizontal":
            summary = tr("{} / 间距 {} px", "{} / {} px gap").format(
                self.cb_mode.currentText(), self.cb_gap.currentText())
            action = tr("开始拼接", "Merge images")
            self.mode_hint.setText(tr(
                "所有图片按顺序向右排列，并等比缩放到整批最大高度，不会裁剪。间距和透明区域使用白色。",
                "Images are placed to the right and scaled proportionally to the batch's tallest image without cropping. Gaps and transparent areas use white."))
        elif mode == "pdf_a4":
            summary = self.cb_mode.currentText()
            action = tr("生成 PDF 相册", "Create PDF album")
            self.mode_hint.setText(tr(
                "每张图片生成一页 A4 白底页面并居中显示；超出页面的图片会等比缩小，小图不会放大。",
                "Each image becomes one centered A4 page on white. Oversized images scale down proportionally; smaller images are not enlarged."))
        else:
            summary = self.cb_mode.currentText()
            action = tr("生成 PDF 相册", "Create PDF album")
            self.mode_hint.setText(tr(
                "每张图片生成一页，并保留各自的原始像素尺寸，因此不同页面的尺寸可能不同。",
                "Each image becomes one page at its original pixel size, so page dimensions may differ."))
        self.file_card.set_target_fmt(summary)
        self.action_bar.btn_go.setText(action)
        files = self.file_card.files()
        output_ext = ".pdf" if mode in ("pdf_a4", "pdf_original") else ".jpg"
        first_name = (os.path.splitext(os.path.basename(files[0]))[0]
                      if files else tr("第一张图片名", "first image name"))
        output_name = f"{first_name}_merged{output_ext}"
        self.output_hint.setText(tr(
            "整批 {count} 张图片只生成 1 个文件：{name}。重名处理沿用全局设置，源图片保持不变。",
            "The batch of {count} images creates one file: {name}. Name conflicts follow global settings; source images stay unchanged.").format(
                count=len(files), name=output_name))

    def _set_inputs_enabled(self, enabled):
        """合成任务使用整批顺序快照；运行中锁住增删与拖拽，避免行状态错位。"""
        for button in (self.file_card.btn_add, self.file_card.btn_add_dir,
                       self.file_card.btn_rm, self.file_card.btn_clear):
            button.setEnabled(enabled)
        for target in (self.file_card, self.file_card.table,
                       self.file_card.table.viewport()):
            target.setAcceptDrops(enabled)
        self.merge_grid.setEnabled(enabled)
        if enabled:
            self.file_card._refresh_actions()

    def _on_progress(self, task_id, pct, msg, speed):
        super()._on_progress(task_id, pct, msg, speed)
        for path in self._merge_task_files.get(task_id, [])[1:]:
            self.file_card.set_row_progress(
                self.file_card.row_of_file(path), pct)

    def _on_state(self, task_id, state):
        files = list(self._merge_task_files.get(task_id, []))
        super()._on_state(task_id, state)
        for path in files[1:]:
            row = self.file_card.row_of_file(path)
            if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
                self.file_card.set_row_progress(row, -1, tm.state_text(state))
            self.file_card.set_row_state(row, tm.state_text(state))
        if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
            self._merge_task_files.pop(task_id, None)
            if not self._task_rows:
                self._set_inputs_enabled(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "settings_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
