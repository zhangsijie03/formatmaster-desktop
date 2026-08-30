"""video_unwarp_panel — 视频反挤压（宽高比修复）面板。

老视频 / 非正方形像素视频画面被压扁拉长时，恢复到正确显示比例。
- 自动修复：按视频自带 DAR 修正 SAR 元数据（流复制，快）
- 手动反挤压：按目标比例（4:3 / 16:9 / 9:16 / 1:1 / 自定义）重编码拉伸
"""

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QSpinBox, QWidget
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, PushButton)

from gui_qt import task_manager as tm
from gui_qt.components.page_header import PageHeader
from gui_qt.components.safe_worker import SafeWorker
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".ts"}

# 目标比例下拉（显示名 ↔ 值）
RATIO_VALUES = [tr("自动修复（按视频 DAR）", "Auto (video DAR)"),
                "16:9", "4:3", "9:16", "1:1",
                tr("自定义…", "Custom…")]
_RATIO_KEYS = ["auto", "16:9", "4:3", "9:16", "1:1", "custom"]


class _DarWorker(SafeWorker):
    """后台读取视频比例，避免 ffprobe 超时阻塞主界面。"""

    sig_done = Signal(str, object)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = path

    def work(self):
        from core.video_unwarp import get_video_dar
        self.sig_done.emit(self._path, get_video_dar(self._path))


class VideoUnwarpPanelPage(BaseQtPanel, TaskPanelMixin):
    """视频反挤压页。"""

    panel_key = "video_unwarp"

    # ── UI ──────────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频反挤压", "Unwarp Video"),
            tr("修复视频画面被压扁/拉长的问题（如老 DVD、非正方形像素视频）",
               "Fix stretched/squashed video (old DVDs, non-square pixels)"),
            FluentIcon.MEDIA))

        self.file_card = FileListCard(tr("视频列表", "Video list"),
                                      file_exts=VIDEO_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.files_changed.connect(self._refresh_info)
        self.file_card.table.itemSelectionChanged.connect(self._refresh_info)

        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)
        self.btn_preview = PushButton(
            FluentIcon.PLAY, tr("预览源视频", "Preview source"))
        self.btn_preview.clicked.connect(self._preview_video)
        preview_row.addWidget(self.btn_preview)
        self.preview_hint = CaptionLabel()
        self.preview_hint.setWordWrap(True)
        self.preview_hint.setSizePolicy(QSizePolicy.Ignored,
                                        QSizePolicy.Preferred)
        preview_row.addWidget(self.preview_hint, 1)
        self.file_card.layout().addLayout(preview_row)

        from gui_qt.components.form_widgets import FormGrid, FormSection
        sec = FormSection(tr("反挤压设置", "Unwarp settings"),
                          FluentIcon.MEDIA)

        self.params_grid = FormGrid(columns=2)
        self.cb_ratio = self.params_grid.add_field(
            tr("目标比例", "Target ratio"), self._ratio_combo(),
            hint=tr("自动修复最快（仅改元数据）；手动比例会重编码拉伸画面",
                    "Auto is fastest (metadata only); manual re-encodes"))
        sec.add_form(self.params_grid)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)

        # 自定义宽高比（仅自定义模式可见）
        self.w_custom = QWidget()
        crow = QHBoxLayout(self.w_custom)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(8)
        crow.addWidget(CaptionLabel(tr("宽", "W")))
        self.sb_w = QSpinBox()
        self.sb_w.setRange(1, 10000)
        self.sb_w.setValue(16)
        crow.addWidget(self.sb_w)
        crow.addWidget(CaptionLabel(tr("高", "H")))
        self.sb_h = QSpinBox()
        self.sb_h.setRange(1, 10000)
        self.sb_h.setValue(9)
        crow.addWidget(self.sb_h)
        crow.addStretch(1)
        sec.add_widget(self.w_custom)
        sec.add_widget(self.mode_hint)

        # 视频信息
        self.lb_info = CaptionLabel(
            tr("导入视频后显示当前分辨率与比例", "Shows resolution & ratio after import"))
        self.lb_info.setProperty("sec", True)
        self.lb_info.setStyleSheet("font-size: 12px;")
        sec.add_widget(self.lb_info)
        lay.addWidget(sec)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始修复", "Unwarp"))
        lay.addWidget(self.action_bar)

        self.cb_ratio.currentIndexChanged.connect(self._ratio_changed)
        self.sb_w.valueChanged.connect(self._sync_target_summary)
        self.sb_h.valueChanged.connect(self._sync_target_summary)
        self._ratio_changed()
        self.services.task_manager.register_runner(
            "video_unwarp", lambda task: self._runner)
        self._wire_tasks()
        self._info_worker = None
        self._pending_info_path = ""
        self._reserved_output_paths = set()
        self._sync_preview_enabled()
        self._sync_target_summary()

    def _ratio_combo(self):
        cb = ComboBox()
        cb.addItems(RATIO_VALUES)
        cb.setCurrentText(RATIO_VALUES[0])
        return cb

    def _ratio_changed(self):
        key = _RATIO_KEYS[self.cb_ratio.currentIndex()]
        self.w_custom.setVisible(key == "custom")
        self._sync_target_summary()

    def _selected_file(self):
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        files = self.file_card.files()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _sync_preview_enabled(self):
        selected = self._selected_file()
        enabled = bool(selected)
        self.btn_preview.setEnabled(enabled)
        self.btn_preview.setToolTip(
            tr("预览当前选中的源视频", "Preview the selected source video")
            if enabled else tr("请先添加视频文件", "Add a video first"))
        self.preview_hint.setText(
            tr("当前预览：{}", "Previewing: {}").format(
                os.path.basename(selected))
            if selected else tr("添加视频后可在修复前检查画面比例。",
                                "Add a video to inspect its aspect ratio before fixing."))
        self.preview_hint.setToolTip(selected)

    def _preview_video(self):
        selected = self._selected_file()
        if not selected:
            return
        from gui_qt.components.video_preview import VideoPreviewDialog
        dialog = VideoPreviewDialog(selected, self.window())
        dialog.exec()

    def _sync_target_summary(self):
        ratio = self._target_ratio()
        count = len(self.file_card.files())
        if ratio == "auto":
            self.file_card.set_target_fmt(tr("元数据修复 · 保留源容器",
                                             "Metadata fix · Source container"))
            self.mode_hint.setText(tr(
                "读取视频自带的显示比例（DAR），仅修正显示比例元数据；不缩放画面、不重新编码，速度快且画质不变。若视频没有有效 DAR，任务会提示改用手动比例。",
                "Uses the video's display aspect ratio (DAR) to update metadata only. No resizing or re-encoding, so it is fast and lossless. If DAR is missing, choose a manual ratio."))
            self.output_hint.setText(tr(
                "整批 {count} 个视频，各保留原容器并生成“视频名_unwarped.原扩展名”；重名沿用全局冲突设置，不修改源文件。",
                "Batch: {count} videos. Each keeps its source container and is named video_unwarped.<source extension>; name conflicts follow global settings. Source files stay unchanged.").format(count=count))
            self.action_bar.btn_go.setText(tr("修复显示比例", "Fix display ratio"))
        else:
            self.file_card.set_target_fmt(tr("画面拉伸 · {ratio} · MP4",
                                             "Resize picture · {ratio} · MP4").format(
                                                 ratio=ratio))
            self.mode_hint.setText(tr(
                "把画面像素拉伸到 {ratio}，并重置为方形像素；视频将以 H.264 重新编码，音频转为 AAC。请先预览确认人物或圆形不再被压扁。",
                "Resizes picture pixels to {ratio} and resets square pixels. Video is re-encoded as H.264 and audio as AAC. Preview first to confirm people and circles no longer look squashed.").format(ratio=ratio))
            self.output_hint.setText(tr(
                "整批 {count} 个视频，各生成“视频名_unwarped.mp4”；手动模式统一输出 MP4，重名沿用全局冲突设置，不修改源文件。",
                "Batch: {count} videos. Each produces video_unwarped.mp4; manual mode always outputs MP4. Name conflicts follow global settings. Source files stay unchanged.").format(count=count))
            self.action_bar.btn_go.setText(tr("按 {} 修复", "Fix to {}").format(ratio))

    def _refresh_info(self):
        self._sync_preview_enabled()
        self._sync_target_summary()
        selected = self._selected_file()
        if not selected:
            self.lb_info.setText(tr("导入视频后显示当前分辨率与比例",
                                    "Shows resolution & ratio after import"))
            return
        self.lb_info.setText(tr("正在读取视频信息…", "Reading video info…"))
        worker = self._info_worker
        if worker is not None and worker.isRunning():
            self._pending_info_path = selected
            return
        self._start_info_worker(selected)

    def _start_info_worker(self, path):
        self._pending_info_path = ""
        worker = _DarWorker(path, self)
        worker.sig_done.connect(self._on_info_ready)
        worker.finished.connect(
            lambda current=worker: self._on_info_finished(current))
        self._info_worker = worker
        worker.start()

    def _on_info_ready(self, path, meta):
        if path != self._selected_file():
            return
        if not meta:
            self.lb_info.setText(tr("无法读取视频信息", "Cannot read video info"))
            return
        w, h, dar, sar = meta
        self.lb_info.setText(
            tr("当前", "Current") + f"  {w}×{h} · "
            + tr("显示比例", "DAR") + f" {dar} · "
            + tr("像素比例", "SAR") + f" {sar}")

    def _on_info_finished(self, worker):
        worker.deleteLater()
        if self._info_worker is not worker:
            return
        self._info_worker = None
        pending = self._pending_info_path
        self._pending_info_path = ""
        if pending and pending == self._selected_file():
            self._start_info_worker(pending)

    # ── 参数 ────────────────────────────────────
    def _target_ratio(self):
        key = _RATIO_KEYS[self.cb_ratio.currentIndex()]
        if key == "auto":
            return "auto"
        if key == "custom":
            return f"{self.sb_w.value()}:{self.sb_h.value()}"
        return key

    def _target_extension(self):
        """手动重编码统一输出 MP4；自动流复制保持源容器。"""
        if self._target_ratio() != "auto":
            return ".mp4"
        selected = self._selected_file()
        return os.path.splitext(selected)[1].lower() or ".mp4"

    def collect_params(self) -> dict:
        return {
            "ratio": self._target_ratio(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "ratio": self._target_ratio(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        r = prefs.get("ratio")
        if r and r in _RATIO_KEYS and r != "custom":
            self.cb_ratio.setCurrentIndex(_RATIO_KEYS.index(r))
            self._ratio_changed()
        elif r and ":" in str(r):
            left, right = str(r).split(":", 1)
            try:
                width, height = int(left), int(right)
            except ValueError:
                width = height = 0
            if 1 <= width <= 10000 and 1 <= height <= 10000:
                self.cb_ratio.setCurrentIndex(_RATIO_KEYS.index("custom"))
                self.sb_w.setValue(width)
                self.sb_h.setValue(height)
                self._ratio_changed()
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务 ────────────────────────────────────
    def _runner(self, task, prog):
        from core.video_unwarp import fix_aspect
        return fix_aspect(task.file_path, task.output_path,
                          task.params.get("ratio", "auto"),
                          progress_cb=prog)

    def _make_task(self, f):
        params = self.collect_params()
        source_ext = os.path.splitext(f)[1].lower() or ".mp4"
        output_ext = source_ext if params["ratio"] == "auto" else ".mp4"
        stem = os.path.splitext(os.path.basename(f))[0] + "_unwarped"
        target = tm.make_output_path(
            f, self.out_row.resolve_dir(f), output_ext, name=stem)
        base, ext = os.path.splitext(target)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(target))
        while normalized in self._reserved_output_paths:
            target = f"{base}_{counter}{ext}"
            normalized = os.path.normcase(os.path.abspath(target))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(
            name=f"{tr('视频反挤压', 'Unwarp')} - {os.path.basename(f)}",
            task_type="video_unwarp", file_path=f, output_path=target,
            params=params, runner=self._runner, runner_key="video_unwarp",
            history_type=tr("视频反挤压", "Unwarp"),
            history_target=params["ratio"], need_ffmpeg=True)

    def _start(self):
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要修复的视频", "Add videos to unwarp first")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "params_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
