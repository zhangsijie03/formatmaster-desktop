"""音频增强页局部体验：模式说明、参数反馈和批量输出。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def enhance_panel(monkeypatch):
    from gui_qt.panels.audio_enhance_panel import AudioEnhancePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = AudioEnhancePanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"mode": "denoise", "strength": "25",
                       "eq_low": 0, "eq_mid": 0, "eq_high": 0,
                       "comp_thr": -20, "comp_ratio": 4})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_enhance_all_modes_show_limit_and_output_suffix(enhance_panel):
    from gui_qt.panels.audio_enhance_panel import MODE_KEYS, MODE_SUFFIXES

    _app, panel = enhance_panel
    for mode in MODE_KEYS:
        panel._set_mode(mode)
        assert panel._current_mode() == mode
        assert panel.lb_hint.text()
        assert MODE_SUFFIXES[mode] + ".m4a" in panel.output_hint.text()
        assert "M4A" in panel.file_card._fmt_text
        assert panel.action_bar.btn_go.text()
    panel._set_mode("vocal")
    assert "AI" in panel.lb_hint.text()
    panel._set_mode("music")
    assert "AI" in panel.lb_hint.text()


def test_enhance_dynamic_parameter_explanations(enhance_panel):
    _app, panel = enhance_panel
    panel._set_mode("denoise")
    panel.cb_strength.setCurrentText("40")
    assert "40" in panel.lb_hint.text()
    assert "40 dB" in panel.file_card._fmt_text
    panel._set_mode("equalizer")
    panel.sl_low.set_value(3)
    panel.sl_mid.set_value(-2)
    panel.sl_high.set_value(1)
    for value in ("+3", "-2", "+1"):
        assert value in panel.lb_hint.text()
        assert value in panel.file_card._fmt_text
    panel._set_mode("compress")
    panel.sl_thr.set_value(-18)
    panel.sl_ratio.set_value(6)
    assert "-18" in panel.lb_hint.text()
    assert "6:1" in panel.lb_hint.text()
    assert "-18 dB / 6:1" in panel.file_card._fmt_text


def test_enhance_batch_count_and_outputs_match_modes(enhance_panel, tmp_path):
    from gui_qt.panels.audio_enhance_panel import MODE_KEYS, MODE_SUFFIXES

    _app, panel = enhance_panel
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    panel.file_card.add_files([str(source)])
    panel.out_row.resolve_dir = lambda _path: str(tmp_path)
    assert "1" in panel.output_hint.text()
    for mode in MODE_KEYS:
        panel._set_mode(mode)
        if mode == "equalizer":
            panel.sl_low.set_value(1)
        panel._reserved_output_paths = set()
        task = panel._make_task(str(source))
        assert task["output_path"].endswith(
            f"voice{MODE_SUFFIXES[mode]}.m4a")
        assert task["params"]["mode"] == mode
        assert task["need_ffmpeg"]
    panel.file_card.clear_files()
    assert "0" in panel.output_hint.text()


def test_enhance_runner_keeps_existing_engine_contracts(enhance_panel, monkeypatch):
    from core import audio_tools

    _app, panel = enhance_panel
    calls = []

    def record(name):
        return lambda *args, **kwargs: calls.append((name, args, kwargs)) or True

    for name in ("denoise", "normalize", "enhance", "extract_vocal",
                 "extract_music", "audio_equalizer", "audio_compress"):
        monkeypatch.setattr(audio_tools, name, record(name))
    cases = {
        "denoise": "denoise", "normalize": "normalize", "both": "enhance",
        "vocal": "extract_vocal", "music": "extract_music",
        "equalizer": "audio_equalizer", "compress": "audio_compress",
    }
    for mode, function_name in cases.items():
        panel._set_mode(mode)
        panel.cb_strength.setCurrentText("30")
        panel.sl_low.set_value(2)
        panel.sl_mid.set_value(-1)
        panel.sl_high.set_value(3)
        panel.sl_thr.set_value(-18)
        panel.sl_ratio.set_value(5)
        task = SimpleNamespace(file_path="in.wav", output_path="out.m4a",
                               params=panel.collect_params())
        assert panel._runner(task, None)
        assert calls[-1][0] == function_name
        assert calls[-1][1][:2] == ("in.wav", "out.m4a")


def test_enhance_page_fits_narrow_window_and_preserves_preferences(enhance_panel):
    app, panel = enhance_panel
    panel.apply_prefs({"mode": "compress", "strength": "30",
                       "eq_low": 2, "eq_mid": -1, "eq_high": 3,
                       "comp_thr": -18, "comp_ratio": 5})
    expected = panel.collect_prefs()
    panel.show()
    for width in (720, 1280):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
        assert panel.lb_hint.wordWrap()
        assert panel.output_hint.wordWrap()
