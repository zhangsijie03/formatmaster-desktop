"""音频处理页局部体验：三种模式说明、批量输出及响应式布局。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def audio_panel(monkeypatch):
    from gui_qt.panels.audio_trim_panel import AudioTrimPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    monkeypatch.setattr(AudioTrimPanelPage, "_start_wave_worker",
                        lambda _self, _path: None)
    panel = AudioTrimPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"mode": "trim", "fade_in": "0", "fade_out": "0",
                       "sil_thr": "-50", "sil_min": "0.5", "pitch": "1"})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_audio_trim_summary_explains_batch_waveform(audio_panel):
    _app, panel = audio_panel
    assert panel.sg_mode.currentRouteKey() == "trim"
    assert "Shift" in panel.mode_hint.text()
    assert "0" in panel.output_hint.text()
    panel.ed_start.setText("00:10")
    panel.ed_end.setText("00:25")
    assert "00:10" in panel.file_card._fmt_text
    assert "00:25" in panel.file_card._fmt_text
    params = panel.collect_params()
    assert params["start_sec"] == 10
    assert params["end_sec"] == 25


def test_audio_silence_summary_tracks_threshold_and_duration(audio_panel):
    _app, panel = audio_panel
    panel.sg_mode.setCurrentItem("silence")
    panel.cb_sil_thr.setCurrentText("-40")
    panel.cb_sil_min.setCurrentText("1.5")
    assert "-40" in panel.mode_hint.text()
    assert "1.5" in panel.mode_hint.text()
    assert "M4A" in panel.file_card._fmt_text
    assert "nosil.m4a" in panel.output_hint.text()
    assert panel.action_bar.btn_go.text()
    assert not panel.w_trim.isVisible()
    assert not panel.w_silence.isHidden()


def test_audio_pitch_summary_tracks_direction_and_octave(audio_panel):
    _app, panel = audio_panel
    panel.sg_mode.setCurrentItem("pitch")
    for pitch in ("-12", "-3", "1", "7", "12"):
        panel.cb_pitch.setCurrentText(pitch)
        shown = pitch if pitch.startswith("-") else f"+{pitch}"
        assert shown in panel.file_card._fmt_text
        assert pitch.lstrip("-") in panel.mode_hint.text()
        assert panel.collect_params()["pitch"] == int(pitch)
    assert "pitch.m4a" in panel.output_hint.text()


def test_audio_outputs_match_each_mode(audio_panel, tmp_path):
    _app, panel = audio_panel
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    panel.out_row.resolve_dir = lambda _path: str(tmp_path)
    panel.ed_start.setText("1")
    panel.ed_end.setText("2")
    expected = {
        "trim": ("voice_trim.wav", "trim"),
        "silence": ("voice_nosil.m4a", "silence"),
        "pitch": ("voice_pitch.m4a", "pitch"),
    }
    for mode, (filename, param_mode) in expected.items():
        panel.sg_mode.setCurrentItem(mode)
        panel._reserved_output_paths = set()
        task = panel._make_task(str(source))
        assert task["output_path"].endswith(filename)
        assert task["params"]["mode"] == param_mode
        assert task["need_ffmpeg"]


def test_audio_runners_keep_existing_engine_contracts(audio_panel, monkeypatch):
    import core.audio_tools as tools
    import core.audio_trimmer as trimmer

    _app, panel = audio_panel
    calls = []
    monkeypatch.setattr(trimmer, "trim_audio",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(tools, "remove_silence",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(tools, "audio_pitch",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    params = {"mode": "trim", "start_sec": 1, "end_sec": 4,
              "fade_in": 0.5, "fade_out": 1.0}
    task = SimpleNamespace(file_path="in.wav", output_path="out.wav", params=params)
    assert panel._runner(task, None)
    assert calls[-1] == (("in.wav", "out.wav"), {
        "start_sec": 1.0, "end_sec": 4.0, "fade_in": 0.5,
        "fade_out": 1.0, "progress_cb": None})
    task.params = {"mode": "silence", "sil_thr": -45, "sil_min": 1.5}
    assert panel._runner(task, None)
    assert calls[-1] == (("in.wav", "out.wav"), {
        "threshold": -45, "min_silence": 1.5, "progress_cb": None})
    task.params = {"mode": "pitch", "pitch": -3}
    assert panel._runner(task, None)
    assert calls[-1] == (("in.wav", "out.wav"), {
        "semitones": -3, "progress_cb": None})


def test_audio_time_form_reflows_without_changing_preferences(audio_panel):
    app, panel = audio_panel
    expected = panel.collect_prefs()
    panel.show()
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 1000)
        app.processEvents()
        assert panel.time_grid._columns == columns
        assert panel.fade_grid._columns == columns
        assert panel.silence_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
        assert panel.mode_hint.wordWrap()
        assert panel.output_hint.wordWrap()
