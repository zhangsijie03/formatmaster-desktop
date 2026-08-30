"""“音频增强”菜单页面审查后的定向回归测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.audio_enhance_panel import AudioEnhancePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = AudioEnhancePanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_modes_update_action_and_target(panel, app):
    from gui_qt.panels.audio_enhance_panel import MODE_KEYS

    assert panel.cb_mode.count() == len(MODE_KEYS)
    panel._set_mode("music")
    app.processEvents()
    assert panel._current_mode() == "music"
    assert panel.action_bar.btn_go.text() in ("开始提取伴奏", "Extract music")
    assert "M4A" in panel.file_card._fmt_text
    assert panel.w_hint.isVisibleTo(panel)


def test_slider_preferences_are_restored(panel):
    panel.apply_prefs({
        "mode": "equalizer", "strength": "40",
        "eq_low": 3.0, "eq_mid": -2.0, "eq_high": 1.0,
        "comp_thr": -18.0, "comp_ratio": 6.0,
    })
    assert panel._current_mode() == "equalizer"
    assert panel.cb_strength.currentText() == "40"
    assert panel.sl_low.value() == 3.0
    assert panel.sl_mid.value() == -2.0
    assert panel.sl_high.value() == 1.0
    assert panel.sl_thr.value() == -18.0
    assert panel.sl_ratio.value() == 6.0


def test_same_named_batch_outputs_are_unique(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "voice.wav"
    second = second_dir / "voice.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel._set_mode("denoise")
    panel._reserved_output_paths.clear()

    task_a = panel._make_task(str(first))
    task_b = panel._make_task(str(second))

    assert task_a["output_path"] != task_b["output_path"]
    assert task_a["output_path"].endswith("voice_denoise.m4a")
    assert task_b["output_path"].endswith("voice_denoise_1.m4a")
    assert task_a["runner_key"] == "audio_enhance"
