# -*- coding: utf-8 -*-
"""video_compress_panel — 视频压缩面板。

HEVC（libx265）恒定质量重编码 + 可选分辨率缩放，把大视频压小；
复用 TaskManager 通用任务链路与 FFmpegProgressReader 进度。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PushButton

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import (ActionBar, ActionStatusState, FileListCard,
                            OutputDirRow)
from utils.config import SUPPORTED_VIDEO
from core.video_compress import CRF_PRESETS, RES_VALUES

VIDEO_EXTS = set(SUPPORTED_VIDEO.values()) - {".gif"}

# 下拉显示名（与 CRF_PRESETS / RES_VALUES 键一一对应）
QUALITY_VALUES = [tr("高质量（体积大）", "High (larger)"),
                  tr("平衡（推荐）", "Balanced (recommended)"),
                  tr("小体积（质量稍降）", "Small (lower quality)")]
QUALITY_KEYS = ["高质量", "平衡", "小体积"]
RES_DISPLAY = [tr("原分辨率", "Original"), "1080p", "720p", "480p"]
RES_KEYS = ["原分辨率", "1080p", "720p", "480p"]
CODEC_VALUES = [tr("HEVC H.265（推荐，更小）", "HEVC H.265 (recommended, smaller)"),
                tr("H.264（兼容性更好）", "H.264 (better compatibility)")]
CODEC_KEYS = ["libx265", "libx264"]
QUALITY_HINTS = {
    "高质量": tr("优先保留画面细节，输出体积通常较大。", "Preserves more detail, usually with larger output files."),
    "平衡": tr("兼顾画质与体积，适合日常压缩。", "Balances quality and size for everyday compression."),
    "小体积": tr("优先减小体积，细节可能损失更多。", "Prioritizes smaller files with more potential detail loss."),
}


class VideoCompressPanelPage(BaseQtPanel):
    """视频压缩页。"""

    panel_key = "video_compress"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频压缩", "Video compress"),
            tr("使用 H.265 / H.264 批量减小视频体积，可按需限制分辨率",
               "Reduce video size in batches with H.265 / H.264 and optional downscaling"),
            FluentIcon.ZIP_FOLDER))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=VIDEO_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt(tr("压缩", "Compress"))

        # 预览与压缩结果属于文件上下文，统一收进文件卡片；避免在卡片外
        # 形成一个无法判断作用对象的孤立按钮。
        prev_row = QHBoxLayout()
        prev_row.setSpacing(8)
        self.btn_preview = PushButton(FluentIcon.PLAY, tr("预览视频", "Preview video"))
        self.btn_preview.setToolTip(tr("内嵌播放器预览选中视频", "Built-in player for selected video"))
        self.btn_preview.clicked.connect(self._preview_video)
        prev_row.addWidget(self.btn_preview)
        self.preview_hint = CaptionLabel(
            tr("预览当前选中的源视频", "Preview the selected source video"))
        self.preview_hint.setProperty("sec", True)
        self.preview_hint.setWordWrap(True)
        self.preview_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        prev_row.addWidget(self.preview_hint, 1)
        self.file_card.layout().addLayout(prev_row)

        # 压缩前后大小对比放在文件上下文内；添加文件即显示输入总量，
        # 批次结束后显示成功任务的汇总结果。
        from gui_qt.components.visual_widgets import SizeCompareBar
        self.size_bar = SizeCompareBar()
        self.size_bar.setFixedHeight(76)
        self.size_bar.setAccessibleName(
            tr("视频压缩前后大小对比", "Video size comparison"))
        self.file_card.layout().addWidget(self.size_bar)
        self.result_hint = CaptionLabel()
        self.result_hint.setWordWrap(True)
        self.file_card.layout().addWidget(self.result_hint)

        lay.addWidget(self._build_params_card())

        from gui_qt.components.form_widgets import FormSection
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        output_hint = CaptionLabel(tr(
            "统一导出 MP4；有音轨时转为 AAC（128 kbps）。",
            "Exports MP4; existing audio is converted to AAC at 128 kbps."))
        output_hint.setWordWrap(True)
        out_card.add_widget(output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始压缩", "Compress"))
        lay.addWidget(self.action_bar)
        self.btn_go = self.action_bar.btn_go
        self.btn_cancel = self.action_bar.btn_cancel
        self.bar_total = self.action_bar.bar_total
        self.status_label = self.action_bar.status_label

        self.btn_go.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel_all)
        self.file_card.files_changed.connect(self._on_files_changed)
        self.file_card.table.itemSelectionChanged.connect(
            self._sync_preview_enabled)
        self.file_card.file_double_clicked.connect(self._preview_video)
        self.cb_quality.currentTextChanged.connect(self._sync_target_summary)
        self.cb_res.currentTextChanged.connect(self._sync_target_summary)
        self.cb_codec.currentTextChanged.connect(self._sync_target_summary)

        mgr = self.services.task_manager
        mgr.sig_progress.connect(self._on_progress)
        mgr.sig_state.connect(self._on_state)
        mgr.register_runner("video_compress", self._restore_runner)
        self._task_rows = {}  # task_id -> (file_path, row)
        self._batch_results = []
        self._batch_progress = {}
        self._completed_sizes = []
        self._sync_target_summary()
        self._refresh_size_preview()
        self._sync_start_enabled()
        self._sync_preview_enabled()

    def _refresh_size_preview(self):
        """添加文件后显示全部输入视频的总大小。"""
        files = self.file_card.files()
        self.size_bar.setVisible(bool(files))
        self.result_hint.setVisible(bool(files))
        if not files:
            self.size_bar.clear()
            self.result_hint.clear()
            return
        try:
            source_total = sum(os.path.getsize(path) for path in files)
            self.size_bar.set_sizes(source_total, 0,
                                    tr("原始合计", "Original total"),
                                    tr("压缩后合计", "Compressed total"))
            self.result_hint.setText(tr(
                "已添加 {} 个视频；大小对比将在处理完成后显示实际结果，并非体积预估。",
                "{} videos added; the comparison shows actual results after processing, not a size estimate.").format(len(files)))
        except OSError:  # noqa: BLE001
            self.size_bar.clear()
            self.size_bar.hide()
            self.result_hint.setText(tr("部分源文件无法读取大小，请确认文件仍可访问。",
                                        "Some source sizes could not be read; check that the files are accessible."))

    def _selected_file(self):
        """返回当前选中视频；未显式选中时回退到第一项。"""
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        files = self.file_card.files()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _preview_video(self, file_path=""):
        """内嵌播放器预览选中的视频（QtMultimedia）。"""
        selected = file_path or self._selected_file()
        if not selected:
            toast.show_warning(self, tr("请先添加视频文件", "Add video files first"))
            return
        from gui_qt.components.video_preview import VideoPreviewDialog
        dlg = VideoPreviewDialog(selected, self.window())
        dlg.exec()

    def _on_files_changed(self):
        self._refresh_size_preview()
        self._sync_start_enabled()
        self._sync_preview_enabled()

    def _sync_preview_enabled(self):
        selected = self._selected_file()
        enabled = bool(selected)
        self.btn_preview.setEnabled(enabled)
        self.btn_preview.setToolTip(
            selected
            if enabled else tr("请先添加视频文件", "Add a video first"))
        self.preview_hint.setText(
            tr("源视频：{}", "Source: {}").format(os.path.basename(selected))
            if selected else tr("添加视频后可预览源文件", "Add a video to preview its source"))
        self.preview_hint.setToolTip(selected)

    def _sync_target_summary(self):
        """文件表格中的转换方向同步当前编码器与分辨率设置。"""
        codec = "H.265" if self.cb_codec.currentText() == CODEC_VALUES[0] else "H.264"
        resolution = self.cb_res.currentText()
        self.file_card.set_target_fmt(f"MP4 · {codec} · {resolution}")
        quality_key = self._key(QUALITY_VALUES, QUALITY_KEYS, self.cb_quality.currentText())
        codec_hint = (
            tr("H.265：通常更省空间，但编码较慢，部分旧设备无法播放。",
               "H.265: usually smaller, but slower to encode and unsupported by some older devices.")
            if self.cb_codec.currentIndex() == 0 else
            tr("H.264：兼容性更广，适合分享或在旧设备播放。",
               "H.264: broader compatibility for sharing and older devices."))
        max_height = RES_VALUES[self._key(RES_DISPLAY, RES_KEYS, resolution)]
        resolution_hint = (
            tr("保留原视频分辨率。", "Keeps the source resolution.")
            if max_height is None else
            tr("将高度限制在 {} px 以内，保持比例，不放大小视频。",
               "Limits height to {} px, preserving aspect ratio without upscaling.").format(max_height))
        self.settings_hint.setText(" ".join((QUALITY_HINTS[quality_key], resolution_hint, codec_hint)))

    def _build_params_card(self):
        from gui_qt.components.form_widgets import FormSection, FormGrid

        sec = FormSection(tr("压缩参数", "Compress settings"), FluentIcon.SETTING)
        self.params_grid = FormGrid(columns=2)

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        self.cb_quality = self.params_grid.add_field(
            tr("压缩等级", "Quality"), _combo(QUALITY_VALUES, QUALITY_VALUES[1]),
            hint=tr("平衡推荐；小体积压缩率最高，画质略降",
                    "Balanced recommended; small gives the highest compression"))
        self.cb_res = self.params_grid.add_field(
            tr("目标分辨率", "Resolution"), _combo(RES_DISPLAY, RES_DISPLAY[0]),
            hint=tr("1080p/720p 可进一步减小体积（不放大原视频）",
                    "Downscale to 1080p/720p for smaller files (never upscales)"))
        self.cb_codec = self.params_grid.add_field(
            tr("编码器", "Codec"), _combo(CODEC_VALUES, CODEC_VALUES[0]),
            hint=tr("HEVC 体积更小；H.264 老设备兼容性更好",
                    "HEVC is smaller; H.264 works on more devices"))
        sec.add_form(self.params_grid)
        self.settings_hint = CaptionLabel()
        self.settings_hint.setWordWrap(True)
        sec.add_widget(self.settings_hint)
        size_notice = CaptionLabel(tr(
            "采用恒定质量压缩，不设目标 MB；已高度压缩的视频可能不会变小。",
            "Uses constant quality, not a target MB size; already-compressed videos may not get smaller."))
        size_notice.setWordWrap(True)
        sec.add_widget(size_notice)
        return sec

    # ── 参数/偏好 ────────────────────────────────
    def _key(self, display_list, key_list, current):
        idx = display_list.index(current) if current in display_list else 0
        return key_list[idx]

    def collect_params(self) -> dict:
        return {
            "crf": CRF_PRESETS[self._key(QUALITY_VALUES, QUALITY_KEYS,
                                         self.cb_quality.currentText())],
            "max_height": RES_VALUES[self._key(RES_DISPLAY, RES_KEYS,
                                               self.cb_res.currentText())],
            "codec": self._key(CODEC_VALUES, CODEC_KEYS,
                               self.cb_codec.currentText()),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "quality": self.cb_quality.currentText(),
            "res": self.cb_res.currentText(),
            "codec": self.cb_codec.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("quality") in QUALITY_VALUES:
            self.cb_quality.setCurrentText(prefs["quality"])
        if prefs.get("res") in RES_DISPLAY:
            self.cb_res.setCurrentText(prefs["res"])
        if prefs.get("codec") in CODEC_VALUES:
            self.cb_codec.setCurrentText(prefs["codec"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _run_compression(self, compressor, task, prog):
        p = task.params
        return compressor.compress(
            task.file_path, task.output_path,
            crf=p.get("crf", 28),
            max_height=p.get("max_height"),
            codec=p.get("codec", "libx265"),
            progress_callback=prog)

    def _new_task_runtime(self):
        """每个任务使用独立压缩器，取消操作不会串扰其他并行任务。"""
        from core.video_compress import VideoCompressor
        compressor = VideoCompressor()

        def runner(task, prog):
            return self._run_compression(compressor, task, prog)

        return runner, compressor.cancel

    def _restore_runner(self, task):
        """为应用重启后手动重试的压缩任务重建执行器与取消句柄。"""
        runner, canceller = self._new_task_runtime()
        task.canceller = canceller
        return runner

    # ── 任务提交 ─────────────────────────────────
    def _start(self):
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加要压缩的视频文件", "Add videos to compress first"))
            return False
        if not self.services.ffmpeg_ready():
            message = tr("FFmpeg 未就绪，请前往“设置 > 高级”重新检测",
                         "FFmpeg is unavailable; recheck it in Settings > Advanced")
            self.action_bar.set_status(message, ActionStatusState.ERROR)
            toast.show_error(self, message)
            return False
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return False

        params = self.collect_params()
        self.save_prefs()
        mgr = self.services.task_manager
        max_retries = int(self.services.get_pref("max_retries", 0) or 0)
        if not self._task_rows:
            self._batch_results = []
            self._batch_progress = {}
            self._completed_sizes = []
        active_files = set()
        for tid, (file_path, _row) in self._task_rows.items():
            task = mgr.get_task(tid)
            if task and task.state in (tm.WAITING, tm.RUNNING, tm.PAUSED):
                active_files.add(file_path)
        added = 0
        used_out = set()  # 同批输出去重（同名视频不同源）
        for f in files:
            if f in active_files:
                continue
            out_dir = self.out_row.resolve_dir(f)
            out_path = tm.make_output_path(f, out_dir, ".mp4")
            base, e = os.path.splitext(out_path)
            n = 1
            while out_path.lower() in used_out:
                out_path = f"{base}_{n}{e}"
                n += 1
            used_out.add(out_path.lower())
            runner, canceller = self._new_task_runtime()
            tid = mgr.add_task(
                name=f"{tr('视频压缩', 'Video Compress')} - {os.path.basename(f)}",
                task_type="video_compress", file_path=f, output_path=out_path,
                params=params, runner=runner, canceller=canceller,
                history_type=tr("视频压缩", "Video Compress"),
                history_target=self.cb_quality.currentText(),
                max_retries=max_retries, runner_key="video_compress")
            if tid is not None:
                self._task_rows[tid] = (f, self.file_card.row_of_file(f))
                self._batch_progress[tid] = 0
                added += 1
        if added:
            # 新批次真正入队后才清掉上一批对比，不能把旧结果当成当前预测值。
            self._refresh_size_preview()
            self.result_hint.setText(tr("正在压缩；实际大小将在本批任务结束后显示。",
                                        "Compressing; actual sizes will appear when this batch finishes."))
            self.action_bar.set_running(True)
            self.action_bar.set_status(
                tr("已提交 {} 个任务", "Submitted {} tasks").format(added))
            return True
        if active_files & set(files):
            self.action_bar.set_status(
                tr("文件已在处理中，已跳过", "Files already processing, skipped"),
                ActionStatusState.WARNING)
        else:
            message = tr("任务提交失败，请检查输出目录和压缩参数",
                         "Task submission failed; check output folder and settings")
            self.action_bar.set_status(message, ActionStatusState.ERROR)
            toast.show_error(self, message)
        return False

    def _cancel_all(self):
        mgr = self.services.task_manager
        for tid in list(self._task_rows):
            mgr.cancel_task(tid)
        self.btn_cancel.setEnabled(False)

    # ── 进度/状态联动 ────────────────────────────
    def _on_progress(self, task_id, pct, msg, speed):
        row = self._task_rows.get(task_id)
        if not row:
            return
        _file, idx = row
        # 文件列表可在运行时变化，按稳定路径定位当前行。
        idx = self.file_card.row_of_file(_file)
        task = self.services.task_manager.get_task(task_id)
        if task and task.state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
            return
        self.file_card.set_row_progress(idx, pct)
        self._batch_progress[task_id] = max(0, min(100, int(pct)))
        self.action_bar.set_status(msg)
        self._update_total()

    def _on_state(self, task_id, state):
        row = self._task_rows.get(task_id)
        if not row:
            return
        if row:
            _file, idx = row
            # 文件列表可在运行时变化，按稳定路径定位当前行。
            idx = self.file_card.row_of_file(_file)
            if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
                self.file_card.set_row_progress(idx, -1, tm.state_text(state))
            self.file_card.set_row_state(idx, tm.state_text(state))
        task = self.services.task_manager.get_task(task_id)
        if state == tm.SUCCESS and task:
            try:
                if os.path.isfile(task.file_path) and os.path.isfile(task.output_path):
                    self._completed_sizes.append((
                        os.path.getsize(task.file_path),
                        os.path.getsize(task.output_path)))
            except OSError:  # noqa: BLE001 - 大小读取失败不影响流程
                pass
        elif state == tm.FAILED and task:
            toast.show_error(self,
                             tr("压缩失败：{}", "Failed: {}").format(os.path.basename(task.file_path))
                             + tr("（{}）", " ({})").format(task.error or tr("未知错误", "unknown error")))
        if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
            self._batch_progress[task_id] = 100
            self._batch_results.append(state)
            self._task_rows.pop(task_id, None)
            self._update_total()
        active = [self.services.task_manager.get_task(t) for t in self._task_rows]
        if not any(t and t.state in (tm.WAITING, tm.RUNNING, tm.PAUSED) for t in active):
            self.action_bar.set_batch_result(
                self._batch_results.count(tm.SUCCESS),
                self._batch_results.count(tm.FAILED),
                self._batch_results.count(tm.CANCELLED))
            if self._completed_sizes:
                before_total = sum(before for before, _after in self._completed_sizes)
                after_total = sum(after for _before, after in self._completed_sizes)
                self.size_bar.show()
                self.result_hint.show()
                self.size_bar.set_sizes(
                    before_total, after_total,
                    tr("成功任务原始合计", "Successful inputs"),
                    tr("压缩后合计", "Compressed total"))
                summary = tr("最近完成批次：仅统计 {} 个成功任务；失败、取消及无法读取大小的文件不计入。",
                             "Last completed batch: {} successful tasks with readable sizes; failed and cancelled files are excluded.").format(len(self._completed_sizes))
                if after_total >= before_total:
                    summary += tr(" 本次未减小体积，可尝试选择“小体积”或降低分辨率。",
                                  " Size did not decrease; try lower quality or resolution.")
                self.result_hint.setText(summary)
            else:
                self.size_bar.clear()
                self.size_bar.hide()
                self.result_hint.show()
                self.result_hint.setText(tr("本批暂无可比较的成功结果，请查看各文件的处理状态。",
                                            "No successful sizes to compare for this batch; check each file's status."))
            self._sync_start_enabled()

    def _update_total(self):
        if not self._batch_progress:
            return
        self.bar_total.setValue(
            sum(self._batch_progress.values()) // len(self._batch_progress))

    def _sync_start_enabled(self):
        """没有输入或批次运行中时禁用主操作，防止重复提交。"""
        enabled = bool(self.file_card.files()) and not self._task_rows
        self.btn_go.setEnabled(enabled)
        if enabled:
            hint = ""
        elif self._task_rows:
            hint = tr("压缩正在进行中", "Compression is in progress")
        else:
            hint = tr("请先添加视频文件", "Add video files first")
        self.btn_go.setToolTip(hint)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "params_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
