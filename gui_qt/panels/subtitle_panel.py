# -*- coding: utf-8 -*-
"""subtitle_panel — 视频字幕提取面板（硬字幕 OCR）。

把视频里的硬字幕 OCR 识别出来生成 .srt 字幕文件
（core.subtitle_extract：FFmpeg 抽帧 + RapidOCR 逐帧识别 + 去重合并时间轴）。
"""
import os
from enum import Enum

from qfluentwidgets import CaptionLabel, CheckBox, ComboBox, FluentIcon

from gui_qt import task_manager as tm
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".wmv", ".mov", ".flv", ".webm", ".ts",
              ".m4v", ".mpg", ".mpeg", ".3gp"}


class SamplingPreset(Enum):
    PRECISE = 2.0
    BALANCED = 1.0
    FAST = 0.5


class SubtitleRegion(str, Enum):
    BOTTOM = "bottom"
    TOP = "top"
    FULL = "full"


FPS_VALUES = [tr("0.5 秒/帧（最准，较慢）", "0.5s/frame (most accurate, slower)"),
              tr("1 秒/帧（推荐）", "1s/frame (recommended)"),
              tr("2 秒/帧（最快，精度较低）", "2s/frame (fastest, less accurate)")]
FPS_PRESETS = [SamplingPreset.PRECISE, SamplingPreset.BALANCED,
               SamplingPreset.FAST]
LANG_VALUES = [tr("中文 / English（本地模型）",
                  "Chinese / English (local model)")]
LANG_KEYS = ["chi_sim+eng"]
REGION_VALUES = [tr("底部字幕区（推荐）", "Bottom (recommended)"),
                 tr("顶部字幕区", "Top"),
                 tr("全屏（可能识别画面文字）",
                    "Full screen (may include on-screen text)")]
REGION_KEYS = [SubtitleRegion.BOTTOM, SubtitleRegion.TOP,
               SubtitleRegion.FULL]
HEIGHT_VALUES = ["10%", "15%", "20%", "25%"]
LEGACY_FPS = {
    "0.5 秒/帧（最快）": SamplingPreset.PRECISE.value,
    "0.5s/frame (fast)": SamplingPreset.PRECISE.value,
    "2 秒/帧（最准）": SamplingPreset.FAST.value,
    "2s/frame (accurate)": SamplingPreset.FAST.value,
}
LEGACY_REGIONS = {
    "底部字幕区（推荐）": SubtitleRegion.BOTTOM.value,
    "Bottom (recommended)": SubtitleRegion.BOTTOM.value,
    "顶部字幕区": SubtitleRegion.TOP.value,
    "Top": SubtitleRegion.TOP.value,
    "全屏（会连屏幕文字一起识别）": SubtitleRegion.FULL.value,
    "Full screen (may include on-screen UI text)": SubtitleRegion.FULL.value,
}


def _fps_value(value):
    """把稳定数值、新界面文案或旧版文案统一为每秒采样帧数。"""
    if isinstance(value, (int, float)):
        return float(value)
    if value in FPS_VALUES:
        return FPS_PRESETS[FPS_VALUES.index(value)].value
    return LEGACY_FPS.get(value, SamplingPreset.BALANCED.value)


def _region_value(value):
    """把稳定 key、新界面文案或旧版文案统一为区域枚举值。"""
    stable = [item.value for item in REGION_KEYS]
    if value in stable:
        return value
    if value in REGION_VALUES:
        return REGION_KEYS[REGION_VALUES.index(value)].value
    return LEGACY_REGIONS.get(value, SubtitleRegion.BOTTOM.value)


class SubtitlePanelPage(BaseQtPanel, TaskPanelMixin):
    """视频字幕提取页。"""

    panel_key = "subtitle"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("字幕提取", "Subtitle extract"),
            tr("把视频里的中英文硬字幕识别成 .srt 文件，适合录播课与本地视频",
               "Extract Chinese/English hard subtitles from videos into .srt files"),
            FluentIcon.LANGUAGE))

        self.file_card = FileListCard(tr("视频列表", "Video list"), file_exts=VIDEO_EXTS)
        lay.addWidget(self.file_card)
        self.source_hint = CaptionLabel(tr(
            "识别画面中已有的中英文硬字幕；不做语音转写，也不直接导出内嵌字幕轨。",
            "Reads Chinese/English subtitles burned into the picture; does not transcribe speech or export embedded subtitle tracks."))
        self.source_hint.setWordWrap(True)
        self.file_card.layout().addWidget(self.source_hint)

        from gui_qt.components.form_widgets import FormGrid, FormSection
        card = FormSection(tr("识别设置", "Extract settings"), FluentIcon.LANGUAGE)
        self.params_grid = FormGrid(columns=2)

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        self.cb_fps = self.params_grid.add_field(
            tr("采样间隔", "Sampling interval"), _combo(FPS_VALUES, FPS_VALUES[1]),
            hint=tr("采样越密时间轴越准，但 OCR 耗时越长",
                    "Denser sampling improves timing but takes longer"))
        self.cb_lang = self.params_grid.add_field(
            tr("识别语言", "Language"), _combo(LANG_VALUES, LANG_VALUES[0]),
            hint=tr("当前内置 RapidOCR 模型支持中文和英文",
                    "The bundled RapidOCR model supports Chinese and English"))
        card.add_form(self.params_grid)
        self.sampling_hint = CaptionLabel()
        self.sampling_hint.setWordWrap(True)
        card.add_widget(self.sampling_hint)
        lay.addWidget(card)

        region_card = FormSection(tr("字幕区域", "Subtitle area"), FluentIcon.LAYOUT)
        self.cb_auto = CheckBox(tr("自动检测字幕条带", "Auto-detect subtitle band"))
        self.cb_auto.setChecked(True)
        region_card.add_widget(self.cb_auto)
        self.region_grid = FormGrid(columns=2)
        self.cb_region = self.region_grid.add_field(
            tr("字幕区域", "Subtitle area"), _combo(REGION_VALUES, REGION_VALUES[0]),
            hint=tr("只识别底部字幕区，避免把屏幕上其他文字当字幕", "OCR the subtitle area only to avoid on-screen UI text"))
        self.cb_height = self.region_grid.add_field(
            tr("区域高度", "Area height"), _combo(HEIGHT_VALUES, "15%"),
            hint=tr("越小越精准；若字幕被截断请调大", "Smaller = more precise; enlarge if subtitles get cut off"))
        region_card.add_form(self.region_grid)
        self.region_hint = CaptionLabel()
        self.region_hint.setWordWrap(True)
        region_card.add_widget(self.region_hint)
        lay.addWidget(region_card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始提取", "Extract"))
        lay.addWidget(self.action_bar)

        self.services.task_manager.register_runner(
            "subtitle", lambda task: self._runner)
        self._wire_tasks()
        for control in (self.cb_fps, self.cb_lang, self.cb_region,
                        self.cb_height):
            control.currentTextChanged.connect(self._sync_target_summary)
        self.cb_auto.toggled.connect(self._sync_target_summary)
        self.file_card.files_changed.connect(self._sync_target_summary)
        self._reserved_output_paths = set()
        self._sync_region_controls()
        self.cb_region.currentTextChanged.connect(self._sync_region_controls)
        self._sync_target_summary()

    def _sync_region_controls(self):
        """全屏模式不使用裁剪高度，禁用无效控件避免参数误导。"""
        full_screen = REGION_KEYS[self.cb_region.currentIndex()] == SubtitleRegion.FULL
        self.cb_height.setEnabled(not full_screen)
        self.cb_height.setToolTip(
            tr("全屏模式不使用区域高度", "Area height is unused in full-screen mode")
            if full_screen else "")

    def _sync_target_summary(self) -> None:
        """解释采样成本及自动检测优先级，不改动用户保存的手动备用参数。"""
        fps = FPS_PRESETS[self.cb_fps.currentIndex()].value
        interval = f"{1 / fps:g}"
        self.sampling_hint.setText(tr(
            "每 {interval} 秒识别 1 帧，每分钟视频约采样 {count} 帧（不是字幕条数）。短暂出现的字幕建议用 0.5 秒/帧；结果仍需校对。",
            "One frame every {interval} sec, about {count} samples per minute of video (not subtitle entries). Use 0.5s/frame for brief subtitles; review the result.").format(interval=interval, count=f"{60 * fps:g}"))
        region = REGION_KEYS[self.cb_region.currentIndex()]
        region_name = {
            SubtitleRegion.BOTTOM: tr("底部", "Bottom"),
            SubtitleRegion.TOP: tr("顶部", "Top"),
            SubtitleRegion.FULL: tr("全屏", "Full screen"),
        }[region]
        area = region_name if region == SubtitleRegion.FULL else f"{region_name} {self.cb_height.currentText()}"
        # 检测成功会覆盖包括“全屏”在内的手动范围，不能把备用区域展示为实际识别区域。
        if self.cb_auto.isChecked():
            summary_area = tr("自动区域", "Auto area")
            detail = tr(
                "优先使用自动检测结果；未检测到字幕条带时，使用备用区域：{area}。如需固定识别范围，请关闭自动检测。",
                "Uses the detected band first; if none is found, falls back to {area}. Turn off auto-detection to use a fixed area.").format(area=area)
        else:
            summary_area = area
            detail = tr("固定识别区域：{area}。", "Fixed OCR area: {area}.").format(area=area)
        if region == SubtitleRegion.FULL:
            detail += tr(" 全屏可能混入画面中的其他文字，区域高度不生效。",
                         " Full screen may include other on-screen text; area height is unused.")
        elif not self.cb_auto.isChecked():
            detail += tr(" 若字幕被截断，请增大区域高度。",
                         " Increase the area height if subtitles are cut off.")
        self.region_hint.setText(detail)
        self.file_card.set_target_fmt(tr("SRT · 每 {interval} 秒 · {area}",
                                         "SRT · Every {interval}s · {area}").format(
                                             interval=interval, area=summary_area))
        self.output_hint.setText(tr(
            "整批 {count} 个视频，各生成 1 份 UTF-8 SRT 字幕（如“视频名.srt”）；重名沿用全局冲突设置，不修改原视频。",
            "Batch: {count} videos. Each produces one UTF-8 SRT file (e.g. video.srt); name conflicts follow global settings. Source videos stay unchanged.").format(count=len(self.file_card.files())))

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "fps": FPS_PRESETS[self.cb_fps.currentIndex()].value,
            "lang": LANG_KEYS[self.cb_lang.currentIndex()],
            "region": REGION_KEYS[self.cb_region.currentIndex()].value,
            "height": self.cb_height.currentText(),
            "auto_detect": self.cb_auto.isChecked(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        fps = prefs.get("fps")
        fps_value = _fps_value(fps)
        values = [preset.value for preset in FPS_PRESETS]
        if fps_value in values:
            self.cb_fps.setCurrentIndex(values.index(fps_value))
        region = prefs.get("region")
        region_values = [item.value for item in REGION_KEYS]
        region_value = _region_value(region)
        self.cb_region.setCurrentIndex(region_values.index(region_value))
        if prefs.get("height") in HEIGHT_VALUES:
            self.cb_height.setCurrentText(str(prefs["height"]))
        if "auto_detect" in prefs:
            self.cb_auto.setChecked(bool(prefs["auto_detect"]))
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core.subtitle_extract import extract_subtitles
        p = task.params
        fps = _fps_value(p.get("fps", SamplingPreset.BALANCED.value))
        region = _region_value(
            p.get("region", SubtitleRegion.BOTTOM.value))
        height = float(p.get("height", "15%").replace("%", "")) / 100.0
        return extract_subtitles(
            task.file_path, task.output_path, fps,
            p.get("lang", "chi_sim+eng"), region, height,
            bool(p.get("auto_detect", True)), prog)

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(f, out_dir, ".srt")
        base, ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(
            name=f"{tr('字幕提取', 'Subtitle extract')} - {os.path.basename(f)}",
            task_type="subtitle", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="subtitle",
            history_type=tr("字幕提取", "Subtitle extract"),
            history_target=LANG_VALUES[self.cb_lang.currentIndex()],
            need_ffmpeg=True)

    def _start(self):
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要提取字幕的视频文件", "Add videos to extract subtitles first")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "params_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
        region_grid = getattr(self, "region_grid", None)
        if region_grid is not None:
            region_grid.set_columns(1 if self.viewport().width() < 820 else 2)
