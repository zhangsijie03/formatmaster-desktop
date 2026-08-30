"""audio_panel — 音频转换面板（阶段2 迁移自 gui/panels/audio_panel.py）。

复用 FileListCard / OutputDirRow 原语；任务经 TaskManager.add_task 通用链路
执行 core.audio_converter.AudioConverter，参数与 tkinter 版 collect_params 一致：
fmt / codec / bitrate / sample_rate / channels / volume。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PushButton, Slider

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import (ActionBar, ActionStatusState, FileListCard,
                            OutputDirRow)
from utils.config import SUPPORTED_AUDIO, SUPPORTED_VIDEO

# 音频格式 → FFmpeg 编码器映射（与 tkinter 版 audio_panel.AUDIO_CODEC_MAP 一致）
AUDIO_CODEC_MAP = {
    "MP3": "libmp3lame", "AAC": "aac", "FLAC": "flac", "WAV": "pcm_s16le",
    "WMA": "wmav2", "OGG": "libvorbis", "M4A": "aac",
    "OPUS": "libopus",
}

# 当前打包的 FFmpeg 不含 libopencore_amrnb。AMR 仍保留为可读取的输入，
# 但不再展示一个必然失败的 AMR 输出入口。
AUDIO_OUTPUT_FORMATS = {
    name: ext for name, ext in SUPPORTED_AUDIO.items() if name != "AMR"
}
LOSSLESS_AUDIO_FORMATS = {"FLAC", "WAV"}
BR_VALUES = ["128k", "192k", "256k", "320k"]
SR_VALUES = [tr("原始", "Original"), "22050", "44100", "48000", "96000"]
CH_VALUES = [tr("原始", "Original"), tr("单声道", "Mono"),
             tr("立体声", "Stereo")]
CH_MAP = {CH_VALUES[0]: None, CH_VALUES[1]: 1, CH_VALUES[2]: 2}
VOLUME_UNCHANGED = 100


class AudioPanelPage(BaseQtPanel):
    """音频转换页。"""

    panel_key = "audio"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("音频转换 / 提取", "Audio convert / extract"),
            tr("转换音频格式，也可添加视频提取音轨",
               "Convert audio, or add a video to extract its audio track"),
            FluentIcon.MUSIC))

        exts = set(SUPPORTED_AUDIO.values()) | set(SUPPORTED_VIDEO.values())
        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=exts)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt("MP3")

        lay.addWidget(self._build_params_card())

        from gui_qt.components.form_widgets import FormSection
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        # 波形预览（pyqtgraph，后台解码不卡 UI）
        lay.addWidget(self._build_waveform_card())

        # 底部操作栏
        self.action_bar = ActionBar(tr("开始转换", "Convert"))
        lay.addWidget(self.action_bar)
        self.btn_go = self.action_bar.btn_go
        self.btn_cancel = self.action_bar.btn_cancel
        self.bar_total = self.action_bar.bar_total
        self.status_label = self.action_bar.status_label

        self.btn_go.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel_all)
        self.cb_fmt.currentTextChanged.connect(self.file_card.set_target_fmt)
        self.cb_fmt.currentTextChanged.connect(self._sync_format_controls)
        self.file_card.files_changed.connect(self._on_files_changed)
        self.file_card.table.itemSelectionChanged.connect(
            self._on_wave_selection_changed)
        self._wave_worker = None
        self._wave_pending = ""
        self._wave_source = ""

        mgr = self.services.task_manager
        mgr.sig_progress.connect(self._on_progress)
        mgr.sig_state.connect(self._on_state)
        mgr.register_runner("audio_convert", self._restore_runner)
        self._task_rows = {}   # task_id -> (file_path, row)
        self._batch_results = []
        self._batch_progress = {}
        self._sync_format_controls()
        self._sync_start_enabled()
        self._sync_wave_button()

    def _build_waveform_card(self):
        """波形预览折叠区：按钮 + pyqtgraph 波形（后台解码）。"""
        from PySide6.QtWidgets import QHBoxLayout
        from qfluentwidgets import PushButton
        from gui_qt.components.form_widgets import (CollapsibleSection,
                                                     FormSection)
        from gui_qt.components.audio_waveform import AudioWaveformWidget

        card = FormSection(tr("波形预览", "Waveform preview"), FluentIcon.MUSIC)
        sec = CollapsibleSection(tr("查看波形", "View waveform"),
                                 hint=tr("预览源文件，不影响转换", "Preview the source; conversion is unchanged"))
        self.wave_section = sec
        sec.btn.clicked.connect(self._on_wave_expanded)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_wave = PushButton(tr("刷新波形", "Refresh waveform"))
        self.btn_wave.clicked.connect(self._show_waveform)
        row.addWidget(self.btn_wave)
        self.lb_wave = CaptionLabel(tr("请先添加音频或视频文件", "Add an audio or video file first"))
        self.lb_wave.setWordWrap(True)
        row.addWidget(self.lb_wave, 1)
        sec.add_widget(self._holder(row))
        self.waveform = AudioWaveformWidget()
        sec.add_widget(self.waveform)
        card.add_widget(sec)
        return card

    def _on_wave_expanded(self, expanded: bool) -> None:
        """展开时按需加载选中文件，折叠重开已生成波形不重复解码。"""
        selected = self._selected_wave_file()
        if expanded and selected and selected != self._wave_source:
            self._show_waveform()

    def _sync_wave_button(self) -> None:
        """空列表与后台解码期间禁止重复请求，结束后保留手动刷新入口。"""
        busy = self._wave_worker is not None and self._wave_worker.isRunning()
        self.btn_wave.setEnabled(bool(self.file_card.files()) and not busy)
        self.btn_wave.setText(tr("正在加载…", "Loading…") if busy else
                              tr("刷新波形", "Refresh waveform"))

    @staticmethod
    def _holder(layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _show_waveform(self):
        fp = self._selected_wave_file()
        if not fp:
            toast.show_warning(self, tr("请先添加音频/视频文件", "Add audio/video files first"))
            return
        if self._wave_worker and self._wave_worker.isRunning():
            return
        self._wave_pending = fp
        self.lb_wave.setText(
            tr("正在解码：{}", "Decoding: {}").format(os.path.basename(fp)))
        from gui_qt.components.safe_worker import SafeWorker
        from PySide6.QtCore import Signal

        class _WaveWorker(SafeWorker):
            sig_done = Signal(str, object)

            def __init__(self, f, parent=None):
                super().__init__(parent)
                self._f = f

            def work(self):
                from gui_qt.components.audio_waveform import _decode_pcm, _bucket_peaks
                samples, dur = _decode_pcm(self._f)
                if samples is None or len(samples) == 0:
                    self.sig_done.emit(self._f, None)
                    return
                peaks = _bucket_peaks(samples, 2000)
                self.sig_done.emit(self._f, (peaks, dur))

        self._wave_worker = _WaveWorker(fp, self)
        self._wave_worker.sig_done.connect(self._on_wave_done)
        self._wave_worker.sig_error.connect(self._on_wave_error)
        self._wave_worker.finished.connect(self._sync_wave_button)
        self._wave_worker.start()
        self._sync_wave_button()

    def _on_wave_done(self, fp, result):
        if fp != self._wave_pending or fp not in self.file_card.files():
            return
        self._wave_pending = ""
        if result is None:
            self.lb_wave.setText(tr("解码失败（文件损坏或非音频）", "Decode failed"))
            return
        peaks, dur = result
        self.waveform.set_peaks(peaks, dur)
        self._wave_source = fp
        self.lb_wave.setText(
            tr("{}，时长 {} 秒", "{}, {} seconds")
            .format(os.path.basename(fp), int(dur)))

    def _on_wave_error(self, message):
        """后台解码异常时结束加载态，避免界面永久停在“正在解码”。"""
        self._wave_pending = ""
        self.lb_wave.setText(
            tr("波形生成失败：{}", "Waveform failed: {}").format(message))

    def _selected_wave_file(self):
        """波形跟随当前选中行；没有显式选择时使用第一项。"""
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        files = self.file_card.files()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _clear_waveform(self):
        self._wave_pending = ""
        self._wave_source = ""
        self.waveform.clear()
        self.lb_wave.setText(
            tr("文件已切换，点击刷新波形", "File changed; refresh the waveform")
            if self.file_card.files() else
            tr("请先添加音频或视频文件", "Add an audio or video file first"))

    def _on_wave_selection_changed(self):
        selected = self._selected_wave_file()
        if ((self._wave_source and selected != self._wave_source)
                or (self._wave_pending and selected != self._wave_pending)):
            self._clear_waveform()

    def _on_files_changed(self):
        self._sync_start_enabled()
        self._sync_wave_button()
        files = self.file_card.files()
        if ((self._wave_source and self._wave_source not in files)
                or (self._wave_pending and self._wave_pending not in files)):
            self._clear_waveform()

    def _build_params_card(self):
        from gui_qt.components.form_widgets import FormSection, FormGrid

        sec = FormSection(tr("转换参数", "Convert settings"), FluentIcon.SETTING)
        self.params_grid = FormGrid(columns=2)

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        self.cb_fmt = self.params_grid.add_field(
            tr("目标格式", "Target format"),
            _combo(list(AUDIO_OUTPUT_FORMATS), "MP3"),
            hint=tr("输出音频格式", "Output audio format"))
        self.cb_br = self.params_grid.add_field(
            tr("比特率", "Bitrate"), _combo(BR_VALUES, "192k"),
            hint=tr("码率越高音质越好，文件也越大", "Higher bitrate = better audio, larger file"))
        self.cb_sr = self.params_grid.add_field(
            tr("采样率（Hz）", "Sample rate (Hz)"), _combo(SR_VALUES, tr("原始", "Original")))
        self.cb_ch = self.params_grid.add_field(
            tr("声道", "Channels"), _combo(CH_VALUES, tr("原始", "Original")))
        sec.add_form(self.params_grid)

        # 音量滑块（20%~200%，默认 100%）
        from PySide6.QtWidgets import QHBoxLayout, QWidget
        vol_box = QWidget()
        vol_row = QHBoxLayout(vol_box)
        vol_row.setSpacing(8)
        vol_lbl = CaptionLabel(tr("音量", "Volume"))
        vol_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600;"
            "border: none; background: transparent;")
        vol_row.addWidget(vol_lbl)
        self.vol_slider = Slider(Qt.Horizontal)
        self.vol_slider.setRange(20, 200)
        self.vol_slider.setValue(VOLUME_UNCHANGED)
        self.vol_slider.setAccessibleName(tr("输出音量（百分比）", "Output volume (percent)"))
        self.vol_label = CaptionLabel("100%")
        self.vol_slider.valueChanged.connect(
            lambda v: self.vol_label.setText(f"{v}%"))
        vol_row.addWidget(self.vol_slider, 1)
        vol_row.addWidget(self.vol_label)
        # 音量会恢复上次偏好，提供精确回到原音量的入口，避免靠拖动猜测。
        self.btn_reset_volume = PushButton(tr("恢复 100%", "Reset to 100%"))
        self.btn_reset_volume.setEnabled(False)
        self.btn_reset_volume.clicked.connect(
            lambda: self.vol_slider.setValue(VOLUME_UNCHANGED))
        self.vol_slider.valueChanged.connect(
            lambda value: self.btn_reset_volume.setEnabled(value != VOLUME_UNCHANGED))
        vol_row.addWidget(self.btn_reset_volume)
        sec.add_widget(vol_box)
        return sec

    def _sync_format_controls(self, *_args):
        """无损格式不使用目标比特率，禁用无效参数避免误导。"""
        lossless = self.cb_fmt.currentText() in LOSSLESS_AUDIO_FORMATS
        self.cb_br.setEnabled(not lossless)
        self.cb_br.setToolTip(
            tr("无损格式不使用比特率设置", "Bitrate does not apply to lossless output")
            if lossless else
            tr("设置目标音频比特率", "Set the target audio bitrate"))

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        fmt = self.cb_fmt.currentText()
        return {
            "fmt": fmt,
            "codec": AUDIO_CODEC_MAP.get(fmt),
            "bitrate": None if fmt in LOSSLESS_AUDIO_FORMATS
            else self.cb_br.currentText(),
            "sample_rate": self.cb_sr.currentText(),
            "channels": self.cb_ch.currentText(),
            "volume": self.vol_slider.value(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "fmt": self.cb_fmt.currentText(),
            "br": self.cb_br.currentText(),
            "sr": self.cb_sr.currentText(),
            "ch": self.cb_ch.currentText(),
            "volume": self.vol_slider.value(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("fmt") in AUDIO_OUTPUT_FORMATS:
            self.cb_fmt.setCurrentText(prefs["fmt"])
        if prefs.get("br") in BR_VALUES:
            self.cb_br.setCurrentText(prefs["br"])
        if prefs.get("sr") in SR_VALUES:
            self.cb_sr.setCurrentText(prefs["sr"])
        if prefs.get("ch") in CH_VALUES:
            self.cb_ch.setCurrentText(prefs["ch"])
        try:
            volume = int(prefs.get("volume", 100))
            self.vol_slider.setValue(max(20, min(200, volume)))
        except (TypeError, ValueError):
            self.vol_slider.setValue(100)
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器（TaskManager 通用链路）────────
    def _run_conversion(self, converter, task, prog):
        p = task.params
        sr = None if p.get("sample_rate", tr("原始", "Original")) == tr("原始", "Original") \
            else int(p["sample_rate"])
        ch = CH_MAP.get(p.get("channels", tr("原始", "Original")))
        return converter.convert(
            task.file_path, task.output_path,
            codec=p.get("codec"), bitrate=p.get("bitrate", "192k"),
            sample_rate=sr, channels=ch,
            volume=p.get("volume", 100), progress_callback=prog)

    def _new_task_runtime(self):
        """每个任务使用独立转换器，取消一个任务不会串扰并行任务。"""
        from core.audio_converter import AudioConverter
        converter = AudioConverter()

        def runner(task, prog):
            return self._run_conversion(converter, task, prog)

        return runner, converter.cancel

    def _restore_runner(self, task):
        """为应用重启后手动重试的音频任务重建执行器和取消句柄。"""
        runner, canceller = self._new_task_runtime()
        task.canceller = canceller
        return runner

    # ── 任务提交 ─────────────────────────────────
    def _start(self):
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加要转换的音频或视频文件", "Add audio or video files to convert first"))
            return
        if not self.services.ffmpeg_ready():
            message = tr("FFmpeg 未就绪，请前往“设置 > 高级”重新检测",
                         "FFmpeg is unavailable; recheck it in Settings > Advanced")
            self.action_bar.set_status(message, ActionStatusState.ERROR)
            toast.show_error(self, message)
            return
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return

        params = self.collect_params()
        self.save_prefs()
        fmt_ext = AUDIO_OUTPUT_FORMATS[params["fmt"]]
        mgr = self.services.task_manager
        max_retries = int(self.services.get_pref("max_retries", 0) or 0)
        if not self._task_rows:
            self._batch_results = []
            self._batch_progress = {}
        active_files = set()
        for tid, (file_path, _row) in self._task_rows.items():
            task = mgr.get_task(tid)
            if task and task.state in (tm.WAITING, tm.RUNNING, tm.PAUSED):
                active_files.add(file_path)
        added = 0
        used_out = set()  # 同批内已分配的输出路径（防同名不同源互相覆盖）
        for f in files:
            if f in active_files:
                continue
            out_dir = self.out_row.resolve_dir(f)
            out_path = tm.make_output_path(f, out_dir, fmt_ext)
            base, e = os.path.splitext(out_path)
            n = 1
            while out_path.lower() in used_out:
                out_path = f"{base}_{n}{e}"
                n += 1
            used_out.add(out_path.lower())
            runner, canceller = self._new_task_runtime()
            tid = mgr.add_task(
                name=f"{tr('音频转换', 'Audio Convert')} - {os.path.basename(f)}",
                task_type="audio", file_path=f, output_path=out_path,
                params=params, runner=runner, canceller=canceller,
                history_type=tr("音频转换", "Audio Convert"),
                history_target=params["fmt"], max_retries=max_retries,
                runner_key="audio_convert")
            if tid is not None:
                self._task_rows[tid] = (f, self.file_card.row_of_file(f))
                self._batch_progress[tid] = 0
                added += 1
        if added:
            self.action_bar.set_running(True)
            self.action_bar.set_status(
                tr("已提交 {} 个任务", "Submitted {} tasks").format(added))
        elif active_files & set(files):
            self.action_bar.set_status(
                tr("文件已在处理中，已跳过", "Files already processing, skipped"),
                ActionStatusState.WARNING)
        else:
            message = tr("任务提交失败，请检查输出目录和转换参数",
                         "Task submission failed; check output folder and settings")
            self.action_bar.set_status(message, ActionStatusState.ERROR)
            toast.show_error(self, message)

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
                # 终态：移除行内进度条，改为显示状态文字（成功/失败/取消）
                self.file_card.set_row_progress(idx, -1,
                                                tm.state_text(state))
            self.file_card.set_row_state(idx, tm.state_text(state))
        task = self.services.task_manager.get_task(task_id)
        if state == tm.FAILED and task:
            # 失败即时提示（具体文件+原因）；成功统一走全局完成通知，
            # 避免与「全部转换完成」重复弹两条提示
            toast.show_error(self,
                             tr("转换失败：{}", "Failed: {}").format(os.path.basename(task.file_path))
                             + tr("（{}）", " ({})").format(task.error or tr("未知错误", "unknown error")))
        if state in (tm.SUCCESS, tm.FAILED, tm.CANCELLED):
            self._batch_progress[task_id] = 100
            self._batch_results.append(state)
            self._task_rows.pop(task_id, None)
            self._update_total()
        active = [self.services.task_manager.get_task(t)
                  for t in self._task_rows]
        if not any(t and t.state in (tm.WAITING, tm.RUNNING, tm.PAUSED)
                   for t in active):
            self.action_bar.set_batch_result(
                self._batch_results.count(tm.SUCCESS),
                self._batch_results.count(tm.FAILED),
                self._batch_results.count(tm.CANCELLED))
            self._sync_start_enabled()

    def _update_total(self):
        if not self._batch_progress:
            return
        self.bar_total.setValue(
            sum(self._batch_progress.values()) // len(self._batch_progress))

    def _sync_start_enabled(self):
        """没有输入文件或正在转换时禁用主操作，避免无效提交。"""
        enabled = bool(self.file_card.files()) and not self._task_rows
        self.btn_go.setEnabled(enabled)
        if enabled:
            hint = ""
        elif self._task_rows:
            hint = tr("转换正在进行中", "Conversion is in progress")
        else:
            hint = tr("请先添加音频或视频文件",
                      "Add audio or video files first")
        self.btn_go.setToolTip(hint)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "params_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
