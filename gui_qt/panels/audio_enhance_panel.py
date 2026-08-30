# -*- coding: utf-8 -*-
"""audio_enhance_panel — 音频增强面板（降噪/响度/人声伴奏提取/EQ/压限）。

基于 FFmpeg 滤镜（core.audio_tools）：
- 降噪 afftdn、响度 loudnorm（EBU R128）
- 人声/伴奏提取 pan（中置声道原理，非 AI）
- 三段均衡器 equalizer、动态压限 acompressor+alimiter
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, Slider

from gui_qt import task_manager as tm
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

AUDIO_EXTS = {".mp3", ".wav", ".wma", ".aac", ".flac", ".ogg", ".m4a", ".amr", ".opus"}
STRENGTH_VALUES = ["10", "20", "25", "30", "40"]

MODES = [
    ("denoise", tr("降噪", "Denoise")),
    ("normalize", tr("响度归一化", "Normalize loudness")),
    ("both", tr("降噪+响度", "Denoise + Normalize")),
    ("vocal", tr("人声提取", "Extract vocal")),
    ("music", tr("伴奏提取", "Extract music")),
    ("equalizer", tr("均衡器", "Equalizer")),
    ("compress", tr("动态压限", "Compress")),
]

MODE_KEYS = [key for key, _label in MODES]
MODE_LABELS = dict(MODES)
MODE_SUFFIXES = {
    "denoise": "_denoise",
    "normalize": "_normalized",
    "both": "_enhanced",
    "vocal": "_vocal",
    "music": "_music",
    "equalizer": "_eq",
    "compress": "_compressed",
}

MODE_ACTIONS = {
    "denoise": tr("开始降噪", "Denoise"),
    "normalize": tr("开始归一化", "Normalize"),
    "both": tr("开始增强", "Enhance"),
    "vocal": tr("开始提取人声", "Extract vocal"),
    "music": tr("开始提取伴奏", "Extract music"),
    "equalizer": tr("开始均衡", "Apply EQ"),
    "compress": tr("开始压限", "Compress"),
}


class _SliderRow(QWidget):
    """滑块 + 数值标签行。"""

    def __init__(self, label, lo, hi, default, step=1, fmt="{:.0f}", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.lb = CaptionLabel(label)
        self.sl = Slider(Qt.Horizontal)
        self.sl.setAccessibleName(label)
        self.sl.setRange(lo * 10, hi * 10)
        self.sl.setValue(int(default * 10))
        self.sl.setSingleStep(int(step * 10))
        self.lb_val = CaptionLabel(fmt.format(default))
        self._fmt = fmt
        self.sl.valueChanged.connect(
            lambda v: self.lb_val.setText(fmt.format(v / 10.0)))
        lay.addWidget(self.lb)
        lay.addWidget(self.sl, 1)
        lay.addWidget(self.lb_val)

    def value(self):
        return self.sl.value() / 10.0

    def set_value(self, value):
        """按滑块自身范围安全恢复持久化数值。"""
        scaled = max(self.sl.minimum(), min(self.sl.maximum(), round(float(value) * 10)))
        self.sl.setValue(scaled)


class AudioEnhancePanelPage(BaseQtPanel, TaskPanelMixin):
    """音频增强页。"""

    panel_key = "audio_enhance"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("音频增强", "Audio Enhance"),
            tr("降噪 · 响度 · 人声/伴奏提取 · 均衡器 · 压限",
               "Denoise · Loudness · Vocal/Music · EQ · Compress"),
            FluentIcon.SPEAKERS)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("音频列表", "Audio files"), file_exts=AUDIO_EXTS)
        lay.addWidget(self.file_card)

        card = FormSection(tr("增强设置", "Enhance settings"), FluentIcon.SPEAKERS)
        # 七种处理模式用下拉选择，避免窄窗口把长分段控件挤到不可读。
        self.mode_grid = FormGrid(columns=1)
        self.cb_mode = ComboBox()
        self.cb_mode.addItems([label for _key, label in MODES])
        self.cb_mode.setCurrentIndex(0)
        self.mode_grid.add_field(
            tr("处理模式", "Mode"), self.cb_mode,
            hint=tr("所有模式统一输出为 AAC 编码的 M4A 文件",
                    "All modes output AAC-encoded M4A files"))
        card.add_form(self.mode_grid)
        self.cb_mode.currentIndexChanged.connect(self._mode_changed)

        # 降噪强度（denoise / both）
        self.w_strength = QWidget()
        srow = QVBoxLayout(self.w_strength)
        srow.setContentsMargins(0, 0, 0, 0)
        self.strength_grid = FormGrid(columns=1)
        self.cb_strength = ComboBox()
        self.cb_strength.addItems(STRENGTH_VALUES)
        self.cb_strength.setCurrentText("25")
        self.strength_grid.add_field(
            tr("降噪强度 (dB)", "Noise reduction (dB)"), self.cb_strength,
            hint=tr("数值越大，降噪越强，也越可能损伤细节",
                    "Higher values remove more noise but may reduce detail"))
        srow.addLayout(self.strength_grid)
        card.add_widget(self.w_strength)

        # 均衡器三频段
        self.w_eq = QWidget()
        veq = QVBoxLayout(self.w_eq)
        veq.setContentsMargins(0, 0, 0, 0)
        veq.setSpacing(6)
        self.sl_low = _SliderRow(tr("低频 (200Hz)", "Low (200Hz)"), -12, 12, 0)
        self.sl_mid = _SliderRow(tr("中频 (1kHz)", "Mid (1kHz)"), -12, 12, 0)
        self.sl_high = _SliderRow(tr("高频 (5kHz)", "High (5kHz)"), -12, 12, 0)
        veq.addWidget(self.sl_low)
        veq.addWidget(self.sl_mid)
        veq.addWidget(self.sl_high)
        card.add_widget(self.w_eq)

        # 压限参数
        self.w_comp = QWidget()
        vco = QVBoxLayout(self.w_comp)
        vco.setContentsMargins(0, 0, 0, 0)
        vco.setSpacing(6)
        self.sl_thr = _SliderRow(tr("阈值 dB", "Threshold dB"), -50, -5, -20)
        self.sl_ratio = _SliderRow(tr("压缩比", "Ratio"), 1, 20, 4)
        vco.addWidget(self.sl_thr)
        vco.addWidget(self.sl_ratio)
        card.add_widget(self.w_comp)

        # 人声/伴奏/响度提示
        self.w_hint = QWidget()
        hrow = QHBoxLayout(self.w_hint)
        hrow.setContentsMargins(0, 0, 0, 0)
        self.lb_hint = CaptionLabel("")
        self.lb_hint.setWordWrap(True)
        self.lb_hint.setProperty("sec", True)
        hrow.addWidget(self.lb_hint)
        card.add_widget(self.w_hint)

        lay.addWidget(card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)

        self.services.task_manager.register_runner(
            "audio_enhance", lambda task: self._runner)
        self._reserved_output_paths = set()
        self._wire_tasks()
        self.file_card.files_changed.connect(self._sync_target_summary)
        self.cb_strength.currentTextChanged.connect(self._sync_target_summary)
        for row in (self.sl_low, self.sl_mid, self.sl_high,
                    self.sl_thr, self.sl_ratio):
            row.sl.valueChanged.connect(self._sync_target_summary)
        self._mode_changed()

    def _current_mode(self):
        index = self.cb_mode.currentIndex()
        return MODE_KEYS[index] if 0 <= index < len(MODE_KEYS) else "denoise"

    def _set_mode(self, mode):
        if mode in MODE_KEYS:
            self.cb_mode.setCurrentIndex(MODE_KEYS.index(mode))

    def _mode_changed(self, *_args):
        mode = self._current_mode()
        self.w_strength.setVisible(mode in ("denoise", "both"))
        self.w_eq.setVisible(mode == "equalizer")
        self.w_comp.setVisible(mode == "compress")
        self.w_hint.setVisible(True)
        self.action_bar.btn_go.setText(MODE_ACTIONS[mode])
        self._sync_target_summary()

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "mode": self._current_mode(),
            "strength": int(self.cb_strength.currentText()),
            "eq_low": self.sl_low.value(),
            "eq_mid": self.sl_mid.value(),
            "eq_high": self.sl_high.value(),
            "comp_thr": self.sl_thr.value(),
            "comp_ratio": self.sl_ratio.value(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        mode = prefs.get("mode")
        if mode in MODE_KEYS:
            self._set_mode(str(mode))
        if str(prefs.get("strength")) in STRENGTH_VALUES:
            self.cb_strength.setCurrentText(str(prefs["strength"]))
        slider_prefs = (
            (self.sl_low, "eq_low"), (self.sl_mid, "eq_mid"),
            (self.sl_high, "eq_high"), (self.sl_thr, "comp_thr"),
            (self.sl_ratio, "comp_ratio"),
        )
        for row, key in slider_prefs:
            try:
                row.set_value(prefs[key])
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core import audio_tools
        p = task.params
        mode = p.get("mode", "denoise")
        if mode == "normalize":
            return audio_tools.normalize(task.file_path, task.output_path,
                                         target_lufs=-14, progress_cb=prog)
        if mode == "both":
            return audio_tools.enhance(task.file_path, task.output_path,
                                       mode="both", strength=p.get("strength", 25),
                                       target_lufs=-14, progress_cb=prog)
        if mode == "vocal":
            return audio_tools.extract_vocal(task.file_path, task.output_path,
                                             prog)
        if mode == "music":
            return audio_tools.extract_music(task.file_path, task.output_path,
                                             prog)
        if mode == "equalizer":
            return audio_tools.audio_equalizer(
                task.file_path, task.output_path,
                low=p.get("eq_low", 0), mid=p.get("eq_mid", 0),
                high=p.get("eq_high", 0), progress_cb=prog)
        if mode == "compress":
            return audio_tools.audio_compress(
                task.file_path, task.output_path,
                threshold=p.get("comp_thr", -20), ratio=p.get("comp_ratio", 4),
                progress_cb=prog)
        return audio_tools.denoise(task.file_path, task.output_path,
                                   strength=p.get("strength", 25), progress_cb=prog)

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        mode = params["mode"]
        stem = os.path.splitext(os.path.basename(f))[0]
        out_path = tm.make_output_path(
            f, out_dir, ".m4a", name=stem + MODE_SUFFIXES[mode])
        base, ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(
            name=f"{tr('音频增强', 'Audio enhance')} - {os.path.basename(f)}",
            task_type="audio_enhance", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="audio_enhance",
            history_type=tr("音频增强", "Audio enhance"),
            history_target=MODE_LABELS.get(mode, mode),
            need_ffmpeg=True)

    def _start(self):
        if self._current_mode() == "equalizer" and not any(
                abs(value) > 0.01 for value in
                (self.sl_low.value(), self.sl_mid.value(), self.sl_high.value())):
            from gui_qt.components import toast
            toast.show_warning(
                self, tr("请至少调整一个均衡器频段",
                         "Adjust at least one equalizer band"))
            return False
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要处理的音频文件", "Add audio files to process first")

    def _sync_target_summary(self, *_args):
        mode = self._current_mode()
        detail = MODE_LABELS[mode]
        if mode in ("denoise", "both"):
            strength = self.cb_strength.currentText()
            detail += f" · {strength} dB"
            if mode == "denoise":
                hint = tr(
                    "自适应降低稳定底噪，当前强度 {strength} dB。数值越大处理越强，也越可能损失人声或乐器细节；建议先用默认值试听。",
                    "Adaptively reduces steady background noise at {strength} dB. Higher values are stronger but may remove voice or instrument detail; preview the default first.").format(strength=strength)
            else:
                hint = tr(
                    "先以 {strength} dB 降低稳定底噪，再统一到 -14 LUFS 并限制峰值；适合同时存在底噪和音量不一致的素材。",
                    "First reduces steady noise at {strength} dB, then normalizes to -14 LUFS with peak limiting; useful when noise and uneven loudness both occur.").format(strength=strength)
        elif mode == "equalizer":
            low, mid, high = (self.sl_low.value(), self.sl_mid.value(),
                              self.sl_high.value())
            detail += f" · {low:+g}/{mid:+g}/{high:+g} dB"
            hint = tr(
                "低频 / 中频 / 高频当前为 {low:+g} / {mid:+g} / {high:+g} dB；正值增强、负值削弱。至少调整一个频段，三个频段均为 0 时不会提交。",
                "Low / mid / high are {low:+g} / {mid:+g} / {high:+g} dB. Positive boosts and negative cuts. Adjust at least one band; all-zero EQ is not submitted.").format(
                    low=low, mid=mid, high=high)
        elif mode == "compress":
            threshold, ratio = self.sl_thr.value(), self.sl_ratio.value()
            detail += f" · {threshold:g} dB / {ratio:g}:1"
            hint = tr(
                "超过 {threshold:g} dB 的部分按 {ratio:g}:1 压缩，并限制峰值，适合改善音量忽大忽小。阈值越低或压缩比越高，效果越明显。",
                "Audio above {threshold:g} dB is compressed at {ratio:g}:1 with peak limiting, helping uneven volume. Lower thresholds or higher ratios sound stronger.").format(
                    threshold=threshold, ratio=ratio)
        elif mode == "normalize":
            hint = tr(
                "把整体响度统一到 -14 LUFS，并将真峰值限制在 -1.5 dB；适合让多段音频听感音量更一致，不用于清除底噪。",
                "Normalizes overall loudness to -14 LUFS and limits true peak to -1.5 dB; useful for consistent perceived volume, but it does not remove noise.")
        elif mode == "vocal":
            hint = tr(
                "增强左右声道共同的中置内容来突出人声。这不是 AI 分轨，结果不是纯人声，居中的鼓、贝斯等也可能保留。",
                "Emphasizes centered content shared by left and right channels. This is not AI stem separation: results are not vocal-only, and centered drums or bass may remain.")
        else:  # music
            hint = tr(
                "通过左右声道相位抵消削弱居中人声。这不是 AI 分轨，仅支持立体声音频；单声道无法处理，混响或偏离中央的人声可能残留。",
                "Reduces centered vocals by left/right phase cancellation. This is not AI stem separation and requires stereo; mono is unsupported, and reverberant or off-center vocals may remain.")
        self.lb_hint.setText(hint)
        self.file_card.set_target_fmt(f"M4A · {detail}")
        self.output_hint.setText(tr(
            "整批 {count} 个音频，各生成“文件名{suffix}.m4a”（AAC）；所有模式统一输出 M4A，重名沿用全局冲突设置，不修改源文件。",
            "Batch: {count} audio files. Each produces filename{suffix}.m4a (AAC). All modes output M4A; name conflicts follow global settings. Source files stay unchanged.").format(
                count=len(self.file_card.files()), suffix=MODE_SUFFIXES[mode]))
