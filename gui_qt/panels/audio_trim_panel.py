"""audio_trim_panel — 音频剪辑面板（阶段2 迁移自 gui/panels/audio_trim_panel.py）。

波形预览 + 起止时间选择 + 淡入淡出。波形加载在后台线程执行
（core.audio_trimmer 走 ffprobe/ffmpeg），裁剪任务经 TaskManager 执行。
"""
import math
import os

from PySide6.QtCore import Qt, Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (FluentIcon, CaptionLabel, ComboBox, LineEdit,
                            PushButton)

from gui_qt.i18n import tr
from gui_qt import task_manager as tm
from gui_qt.components.form_widgets import FormSection, FormGrid
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

FADE_VALUES = ["0", "0.5", "1.0", "2.0", "3.0", "5.0"]

AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a",
              ".wma", ".opus"}

MODES = [
    ("trim", tr("裁剪", "Trim")),
    ("silence", tr("去静音", "Remove silence")),
    ("pitch", tr("变调", "Change pitch")),
]

MODE_ACTIONS = {
    "trim": tr("开始裁剪", "Trim"),
    "silence": tr("开始去静音", "Remove silence"),
    "pitch": tr("开始变调", "Change pitch"),
}


def parse_time(s):
    """「HH:MM:SS / MM:SS / 秒数」→ 秒；格式非法时返回 None。"""
    try:
        text = (s or "").strip()
        if not text:
            return None
        parts = text.split(":")
        if len(parts) == 3:
            hour, minute, second = int(parts[0]), int(parts[1]), float(parts[2])
            if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
                return None
            value = hour * 3600 + minute * 60 + second
        elif len(parts) == 2:
            minute, second = int(parts[0]), float(parts[1])
            if minute < 0 or not 0 <= second < 60:
                return None
            value = minute * 60 + second
        elif len(parts) == 1:
            value = float(text)
        elif len(parts) != 3:
            return None
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class _WaveWorker(SafeWorker):
    """后台读取音频信息与波形振幅数据，避免阻塞 UI 线程。"""

    sig_done = Signal(str, object, list)  # (文件路径, info, 波形数据)

    def __init__(self, fp, points=300, parent=None):
        super().__init__(parent)
        self._fp = fp
        self._points = points

    def work(self):
        from core.audio_trimmer import get_audio_info, get_waveform_data
        # 探测函数以 None/空列表表达可预期失败；意外异常交由 SafeWorker
        # 统一记录，避免后台线程静默吞错。
        info = get_audio_info(self._fp)
        data = get_waveform_data(self._fp, self._points)
        self.sig_done.emit(self._fp, info, data)



class WaveformWidget(QWidget):
    """振幅条形波形图；左键设置开始时间，Shift+左键设置结束时间。"""

    time_picked = Signal(float, bool)  # (秒, 是否为结束点)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = []
        self.duration = 0.0
        self.start_sec = 0.0
        self.end_sec = 0.0
        self.setFixedHeight(120)

    def set_wave(self, data, duration):
        self.data = list(data or [])
        self.duration = float(duration or 0.0)
        self.update()

    def set_marks(self, start_sec, end_sec):
        self.start_sec = start_sec
        self.end_sec = end_sec
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mid = h // 2
        if not self.data:
            p.setPen(QColor(128, 128, 128))
            p.drawText(self.rect(), Qt.AlignCenter, tr("（点击「刷新波形」加载）", "(click \"Refresh wave\" to load)"))
            return
        n = len(self.data)
        bar_w = max(1.0, w / n)
        pen = QPen(QColor(96, 140, 255), max(1, int(bar_w)))
        p.setPen(pen)
        for i, val in enumerate(self.data):
            x = int(i * bar_w)
            bar_h = max(1, int(val * (mid - 4)))
            p.drawLine(x, mid - bar_h, x, mid + bar_h)
        if self.duration > 0:
            x1 = int(self.start_sec / self.duration * w)
            x2 = int(self.end_sec / self.duration * w)
            p.setPen(QPen(QColor(80, 200, 120), 2))
            p.drawLine(x1, 0, x1, h)
            p.setPen(QPen(QColor(230, 90, 90), 2))
            p.drawLine(x2, 0, x2, h)

    def mousePressEvent(self, e):
        if self.duration <= 0 or e.button() != Qt.LeftButton:
            return
        sec = max(0.0, min(e.position().x() / max(self.width(), 1)
                           * self.duration, self.duration))
        is_end = bool(e.modifiers() & Qt.ShiftModifier)
        self.time_picked.emit(round(sec, 2), is_end)


class AudioTrimPanelPage(BaseQtPanel, TaskPanelMixin):
    """音频剪辑页。"""

    panel_key = "audio_edit"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("音频处理", "Audio Tools"),
            tr("裁剪音频、移除静音片段，或在保持时长的同时改变音高",
               "Trim audio, remove silent sections, or change pitch while preserving duration"),
            FluentIcon.HEADPHONE)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=AUDIO_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.files_changed.connect(self._on_files_changed)
        self.file_card.table.itemSelectionChanged.connect(self._on_files_changed)

        card = FormSection(tr("音频处理", "Audio settings"), FluentIcon.CUT)

        # 模式分段选择
        from qfluentwidgets import SegmentedWidget
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(CaptionLabel(tr("处理模式", "Mode")))
        self.sg_mode = SegmentedWidget()
        for key, label in MODES:
            self.sg_mode.addItem(key, label)
        self.sg_mode.setCurrentItem("trim")
        self.sg_mode.currentItemChanged.connect(
            lambda _k: self._mode_changed())
        mode_row.addWidget(self.sg_mode, 1)
        card.add_layout(mode_row)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        card.add_widget(self.mode_hint)

        # 裁剪区：文件信息 + 波形 + 起止 + 淡入淡出
        self.w_trim = QWidget()
        vtrim = QVBoxLayout(self.w_trim)
        vtrim.setContentsMargins(0, 0, 0, 0)
        vtrim.setSpacing(8)

        # 文件信息行
        info_wrap = QWidget()
        info_row = QHBoxLayout(info_wrap)
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(8)
        self.lb_file = CaptionLabel(tr("未选择音频文件", "No audio file selected"))
        info_row.addWidget(self.lb_file)
        info_row.addStretch(1)
        self.lb_info = CaptionLabel("")
        info_row.addWidget(self.lb_info)
        vtrim.addWidget(info_wrap)

        # 波形预览
        self.wave = WaveformWidget()
        vtrim.addWidget(self.wave)
        self.wave.time_picked.connect(self._on_pick)

        # 起止点使用统一表单网格，避免窄窗口把时间与操作按钮挤在一行。
        self.time_grid = FormGrid(columns=2)
        self.ed_start = LineEdit()
        self.ed_start.setText("00:00:00")
        self.ed_start.textChanged.connect(self._marks_changed)
        self.time_grid.add_field(
            tr("开始时间", "Start time"), self.ed_start,
            hint=tr("支持秒数、MM:SS 或 HH:MM:SS", "Seconds, MM:SS, or HH:MM:SS"))
        self.ed_end = LineEdit()
        self.ed_end.setText("00:00:00")
        self.ed_end.textChanged.connect(self._marks_changed)
        self.time_grid.add_field(
            tr("结束时间", "End time"), self.ed_end,
            hint=tr("必须晚于开始时间", "Must be later than the start time"))
        vtrim.addLayout(self.time_grid)

        time_wrap = QWidget()
        time_row = QHBoxLayout(time_wrap)
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(8)
        self.lb_dur = CaptionLabel(tr("时长: --", "Duration: --"))
        time_row.addWidget(self.lb_dur)
        time_row.addStretch(1)
        self.btn_refresh = PushButton(tr("刷新波形", "Refresh wave"))
        self.btn_refresh.clicked.connect(self._refresh_waveform)
        time_row.addWidget(self.btn_refresh)
        self.btn_editor = PushButton(
            FluentIcon.HEADPHONE, tr("打开波形编辑器", "Open Waveform Editor"))
        self.btn_editor.setToolTip(
            tr("在独立窗口中可视化编辑入/出点", "Edit in/out visually in a standalone window"))
        self.btn_editor.clicked.connect(self._open_wave_editor)
        time_row.addWidget(self.btn_editor)
        vtrim.addWidget(time_wrap)

        # 淡入淡出
        self.fade_grid = FormGrid(columns=2)
        self.cb_fade_in = self.fade_grid.add_field(
            tr("淡入(秒)", "Fade-in (sec)"), self._fade_combo("0"),
            hint=tr("淡入时长，0 表示不淡入", "Fade-in duration, 0 = none"))
        self.cb_fade_out = self.fade_grid.add_field(
            tr("淡出(秒)", "Fade-out (sec)"), self._fade_combo("0"),
            hint=tr("淡出时长，0 表示不淡出", "Fade-out duration, 0 = none"))
        vtrim.addLayout(self.fade_grid)
        card.add_widget(self.w_trim)

        # 去静音区
        self.w_silence = QWidget()
        vs = QVBoxLayout(self.w_silence)
        vs.setContentsMargins(0, 0, 0, 0)
        vs.setSpacing(8)
        self.silence_grid = FormGrid(columns=2)
        self.cb_sil_thr = self._fade_combo("-50")
        self.cb_sil_thr.clear()
        self.cb_sil_thr.addItems(["-40", "-45", "-50", "-55", "-60"])
        self.cb_sil_thr.setCurrentText("-50")
        self.cb_sil_min = self._fade_combo("0.5")
        self.cb_sil_min.clear()
        self.cb_sil_min.addItems(["0.2", "0.5", "1.0", "1.5", "2.0"])
        self.cb_sil_min.setCurrentText("0.5")
        self.silence_grid.add_field(tr("静音阈值(dB)", "Silence threshold (dB)"), self.cb_sil_thr)
        self.silence_grid.add_field(tr("最短静音(秒)", "Min silence (sec)"), self.cb_sil_min)
        vs.addLayout(self.silence_grid)
        card.add_widget(self.w_silence)

        # 变调区
        self.w_pitch = QWidget()
        vp = QVBoxLayout(self.w_pitch)
        vp.setContentsMargins(0, 0, 0, 0)
        self.pitch_grid = FormGrid(columns=1)
        self.cb_pitch = ComboBox()
        self.cb_pitch.addItems(["-12", "-7", "-5", "-3", "-1",
                                "1", "3", "5", "7", "12"])
        self.cb_pitch.setCurrentText("1")
        self.pitch_grid.add_field(
            tr("变调(半音)", "Pitch (semitones)"), self.cb_pitch,
            hint=tr("正数升调，负数降调；0 不产生变化，因此不提供",
                    "Positive raises pitch, negative lowers it; zero makes no change"))
        vp.addLayout(self.pitch_grid)
        card.add_widget(self.w_pitch)

        lay.addWidget(card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始裁剪", "Trim"))
        lay.addWidget(self.action_bar)

        self._wave_worker = None
        self._pending_wave_path = ""
        self._reserved_output_paths = set()
        self.services.task_manager.register_runner(
            "audio_trim", lambda task: self._runner)
        self._wire_tasks()
        for widget in (self.ed_start, self.ed_end, self.cb_fade_in,
                       self.cb_fade_out, self.cb_sil_thr, self.cb_sil_min,
                       self.cb_pitch):
            signal = getattr(widget, "textChanged", None)
            if signal is None:
                signal = widget.currentTextChanged
            signal.connect(self._sync_target_summary)
        self._mode_changed()

    def _mode_changed(self):
        mode = self.sg_mode.currentRouteKey()
        self.w_trim.setVisible(mode == "trim")
        self.w_silence.setVisible(mode == "silence")
        self.w_pitch.setVisible(mode == "pitch")
        self.action_bar.btn_go.setText(MODE_ACTIONS.get(mode, tr("开始处理", "Process")))
        self._sync_target_summary()

    # ── 波形编辑器（独立窗口）────────────────────
    def _open_wave_editor(self):
        selected = self._selected_file()
        if not selected:
            from gui_qt.components import toast
            toast.show_warning(self, tr("请先添加要裁剪的音频",
                                        "Add an audio file first"))
            return
        from gui_qt.components.audio_editor import WaveformEditorDialog
        dlg = WaveformEditorDialog(
            selected,
            start=parse_time(self.ed_start.text()) or 0.0,
            end=parse_time(self.ed_end.text()),
            parent=self)
        if dlg.exec() == QDialog.Accepted:
            st, et = dlg.clip_range()
            if st is not None:
                self.ed_start.setText(f"{st:.2f}")
            if et is not None and et > 0:
                self.ed_end.setText(f"{et:.2f}")
            self._marks_changed()
            # 统一走参数校验与防重复提交流程，避免独立编辑器绕过保护。
            self._start()

    # ── 波形加载 ─────────────────────────────────
    def _on_files_changed(self):
        self._refresh_waveform()
        self._sync_target_summary()

    def _selected_file(self):
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _refresh_waveform(self):
        selected = self._selected_file()
        if not selected:
            self._pending_wave_path = ""
            self.lb_file.setText(tr("未选择音频文件", "No audio file selected"))
            self.lb_info.setText("")
            self.lb_dur.setText(tr("时长: --", "Duration: --"))
            self.wave.set_wave([], 0.0)
            return
        self.lb_file.setText(os.path.basename(selected))
        worker = self._wave_worker
        if worker is not None and worker.isRunning():
            self._pending_wave_path = selected
            return
        self._start_wave_worker(selected)

    def _start_wave_worker(self, path):
        self._pending_wave_path = ""
        worker = _WaveWorker(path, 300, self)
        worker.sig_done.connect(self._on_wave_done)
        worker.finished.connect(
            lambda current=worker: self._on_wave_finished(current))
        self._wave_worker = worker
        worker.start()

    def _on_wave_finished(self, worker):
        worker.deleteLater()
        if self._wave_worker is not worker:
            return
        self._wave_worker = None
        pending = self._pending_wave_path
        self._pending_wave_path = ""
        if pending and pending == self._selected_file():
            self._start_wave_worker(pending)

    def _on_wave_done(self, fp, info, data):
        if self._selected_file() != fp:
            return  # 文件已变化，丢弃过期结果
        duration = 0.0
        if info:
            duration = float(info.get("duration") or 0.0)
            self.lb_info.setText(
                f"{info.get('codec', '')} · {info.get('sample_rate', '')}Hz" +
                f" · {info.get('channels', '')}ch")
            self.lb_dur.setText(tr("时长: {:.1f}s", "Duration: {:.1f}s").format(duration))
            self.ed_end.setText(f"{duration:.2f}")
        self.wave.set_wave(data, duration)
        self._marks_changed()

    def _marks_changed(self, *_a):
        self.wave.set_marks(parse_time(self.ed_start.text()) or 0.0,
                            parse_time(self.ed_end.text()) or 0.0)
        self._sync_target_summary()

    def _on_pick(self, sec, is_end):
        if is_end:
            self.ed_end.setText(f"{sec:.2f}")
        else:
            self.ed_start.setText(f"{sec:.2f}")

    def _fade_combo(self, default):
        cb = ComboBox()
        cb.addItems(FADE_VALUES)
        cb.setCurrentText(default)
        return cb

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "mode": self.sg_mode.currentRouteKey(),
            "start_sec": parse_time(self.ed_start.text()),
            "end_sec": parse_time(self.ed_end.text()),
            "fade_in": float(self.cb_fade_in.currentText()),
            "fade_out": float(self.cb_fade_out.currentText()),
            "sil_thr": float(self.cb_sil_thr.currentText()),
            "sil_min": float(self.cb_sil_min.currentText()),
            "pitch": int(self.cb_pitch.currentText()),
        }

    def collect_prefs(self) -> dict:
        return {
            "mode": self.sg_mode.currentRouteKey(),
            "fade_in": self.cb_fade_in.currentText(),
            "fade_out": self.cb_fade_out.currentText(),
            "sil_thr": self.cb_sil_thr.currentText(),
            "sil_min": self.cb_sil_min.currentText(),
            "pitch": self.cb_pitch.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        mode = prefs.get("mode")
        if mode in dict(MODES):
            self.sg_mode.setCurrentItem(str(mode))
            self._mode_changed()
        if prefs.get("fade_in") in FADE_VALUES:
            self.cb_fade_in.setCurrentText(prefs["fade_in"])
        if prefs.get("fade_out") in FADE_VALUES:
            self.cb_fade_out.setCurrentText(prefs["fade_out"])
        if prefs.get("sil_thr") in ["-40", "-45", "-50", "-55", "-60"]:
            self.cb_sil_thr.setCurrentText(prefs["sil_thr"])
        if prefs.get("sil_min") in ["0.2", "0.5", "1.0", "1.5", "2.0"]:
            self.cb_sil_min.setCurrentText(prefs["sil_min"])
        if prefs.get("pitch") in ["-12", "-7", "-5", "-3", "-1", "1", "3", "5", "7", "12"]:
            self.cb_pitch.setCurrentText(prefs["pitch"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        mode = p.get("mode", "trim")
        if mode == "silence":
            from core.audio_tools import remove_silence
            return remove_silence(task.file_path, task.output_path,
                                  threshold=p.get("sil_thr", -50),
                                  min_silence=p.get("sil_min", 0.5),
                                  progress_cb=prog)
        if mode == "pitch":
            from core.audio_tools import audio_pitch
            return audio_pitch(task.file_path, task.output_path,
                               semitones=p.get("pitch", 0), progress_cb=prog)
        from core.audio_trimmer import trim_audio
        return trim_audio(
            task.file_path, task.output_path,
            start_sec=float(p.get("start_sec", 0)),
            end_sec=float(p.get("end_sec", 0)),
            fade_in=float(p.get("fade_in", 0)),
            fade_out=float(p.get("fade_out", 0)),
            progress_cb=prog)

    def _make_task(self, f):
        params = self.collect_params()
        nm = os.path.splitext(os.path.basename(f))[0]
        source_ext = os.path.splitext(f)[1].lower()
        out_dir = self.out_row.resolve_dir(f)
        mode = params["mode"]
        suffix = {"trim": "_trim", "silence": "_nosil", "pitch": "_pitch"}.get(mode, "_out")
        # 统一走 make_output_path：应用冲突策略（自动改名/覆盖）+ 源目同路径保护
        # 滤镜模式统一编码 AAC，使用兼容的 M4A 容器；纯裁剪保持源容器。
        output_ext = source_ext if mode == "trim" else ".m4a"
        out_path = tm.make_output_path(f, out_dir, output_ext, name=nm + suffix)
        base, ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        target = dict(MODES).get(mode, tr("裁剪", "Trim"))
        return dict(
            name=f"{tr('音频处理', 'Audio')} - {os.path.basename(f)}",
            task_type="audio_trim", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="audio_trim",
            history_type=tr("音频处理", "Audio tools"), history_target=target,
            need_ffmpeg=True)

    def _start(self):
        if self.sg_mode.currentRouteKey() == "trim":
            start = parse_time(self.ed_start.text())
            end = parse_time(self.ed_end.text())
            if start is None or end is None:
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("请输入有效时间（秒、MM:SS 或 HH:MM:SS）",
                             "Enter a valid time (seconds, MM:SS, or HH:MM:SS)"))
                return False
            if end <= 0 or start >= end:
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("结束时间必须大于开始时间",
                             "End time must be later than start time"))
                return False
            clip_duration = end - start
            if (float(self.cb_fade_in.currentText()) > clip_duration
                    or float(self.cb_fade_out.currentText()) > clip_duration):
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("淡入或淡出时长不能超过裁剪片段时长",
                             "Fade duration cannot exceed the trimmed clip"))
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
        mode = self.sg_mode.currentRouteKey()
        count = len(self.file_card.files())
        if mode == "silence":
            target = tr("M4A · 去静音", "M4A · Remove silence")
            self.mode_hint.setText(tr(
                "删除音量低于 {threshold} dB、持续至少 {duration} 秒的静音段，并拼接保留内容。阈值越接近 0，越容易把轻声内容判为静音。",
                "Removes quiet sections below {threshold} dB lasting at least {duration} sec, then joins the remaining audio. Thresholds closer to 0 may remove quiet speech.").format(
                    threshold=self.cb_sil_thr.currentText(),
                    duration=self.cb_sil_min.currentText()))
            output = tr(
                "整批 {count} 个音频，各生成“文件名_nosil.m4a”（AAC）；重名沿用全局冲突设置，不修改源文件。",
                "Batch: {count} audio files. Each produces filename_nosil.m4a (AAC); name conflicts follow global settings. Source files stay unchanged.")
        elif mode == "pitch":
            pitch = self.cb_pitch.currentText()
            signed_pitch = f"+{pitch}" if not pitch.startswith("-") else pitch
            target = tr("M4A · {} 半音", "M4A · {} semitones").format(
                signed_pitch)
            direction = tr("升高", "raises") if not pitch.startswith("-") else tr("降低", "lowers")
            self.mode_hint.setText(tr(
                "音高{direction} {amount} 个半音，同时校正播放速度以保持原时长；±12 个半音等于一个八度。",
                "{direction} pitch by {amount} semitones while correcting speed to preserve duration; ±12 semitones equals one octave.").format(
                    direction=direction, amount=pitch.lstrip("-")))
            output = tr(
                "整批 {count} 个音频，各生成“文件名_pitch.m4a”（AAC）；重名沿用全局冲突设置，不修改源文件。",
                "Batch: {count} audio files. Each produces filename_pitch.m4a (AAC); name conflicts follow global settings. Source files stay unchanged.")
        else:
            # 每行源容器可能不同，目标列以“原格式”表达逐文件保持容器，
            # 避免把当前选中行的扩展名错误套到整批文件。
            target = tr("原格式 · {} 至 {}", "Source · {} to {}").format(
                self.ed_start.text(), self.ed_end.text())
            self.mode_hint.setText(tr(
                "绿色线为开始点，红色线为结束点；单击波形设置开始，Shift+单击设置结束。当前时间范围会应用到整批文件，波形只预览当前选中项。",
                "Green marks the start and red marks the end. Click the waveform for start; Shift-click for end. The range applies to the batch; the waveform previews only the selected file."))
            output = tr(
                "整批 {count} 个音频，各按相同时间范围生成“文件名_trim.原扩展名”；重名沿用全局冲突设置，不修改源文件。",
                "Batch: {count} audio files. Each uses the same time range and produces filename_trim.<source extension>; name conflicts follow global settings. Source files stay unchanged.")
        self.file_card.set_target_fmt(target)
        self.output_hint.setText(output.format(count=count))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 1 if self.viewport().width() < 820 else 2
        for grid in (getattr(self, "fade_grid", None),
                     getattr(self, "silence_grid", None),
                     getattr(self, "time_grid", None)):
            if grid is not None:
                grid.set_columns(columns)
