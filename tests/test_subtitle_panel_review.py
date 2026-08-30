"""字幕页局部体验：采样成本、自动区域优先级与输出契约。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def subtitle_panel(monkeypatch):
    from gui_qt.panels.subtitle_panel import SubtitlePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = SubtitlePanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"fps": 1.0, "region": "bottom", "height": "15%", "auto_detect": True})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_subtitle_sampling_summary_uses_interval_not_fps(subtitle_panel):
    from gui_qt.panels.subtitle_panel import FPS_PRESETS

    _app, panel = subtitle_panel
    for index, preset in enumerate(FPS_PRESETS):
        panel.cb_fps.setCurrentIndex(index)
        assert f"{1 / preset.value:g}" in panel.sampling_hint.text()
        assert f"{60 * preset.value:g}" in panel.sampling_hint.text()
        assert panel.collect_params()["fps"] == preset.value
        assert f"{1 / preset.value:g}" in panel.file_card._fmt_text


def test_subtitle_auto_summary_tracks_fallback_without_resetting_it(subtitle_panel):
    from gui_qt.i18n import tr

    _app, panel = subtitle_panel
    panel.cb_height.setCurrentText("25%")
    panel.cb_region.setCurrentIndex(1)
    assert "25%" in panel.region_hint.text()
    assert tr("自动区域", "Auto area") in panel.file_card._fmt_text
    panel.cb_auto.setChecked(False)
    assert tr("顶部", "Top") in panel.file_card._fmt_text
    assert "25%" in panel.file_card._fmt_text
    panel.cb_region.setCurrentIndex(2)
    assert not panel.cb_height.isEnabled()
    assert "25%" not in panel.region_hint.text()
    assert tr("全屏", "Full screen") in panel.file_card._fmt_text
    panel.cb_auto.setChecked(True)
    assert tr("自动区域", "Auto area") in panel.file_card._fmt_text
    assert tr("备用区域", "falls back to") in panel.region_hint.text()
    panel.cb_region.setCurrentIndex(0)
    assert panel.cb_height.isEnabled()
    assert panel.collect_params()["height"] == "25%"


def test_subtitle_batch_summary_and_task_contract(subtitle_panel, tmp_path):
    _app, panel = subtitle_panel
    assert "0" in panel.output_hint.text()
    assert not panel.action_bar.btn_go.isEnabled()
    sources = [tmp_path / "one.mp4", tmp_path / "two.mp4"]
    for source in sources:
        source.write_bytes(b"video")
    panel.file_card.add_files([str(source) for source in sources])
    assert "2" in panel.output_hint.text()
    assert "UTF-8 SRT" in panel.output_hint.text()
    assert panel.action_bar.btn_go.isEnabled()
    panel.out_row.set_state(panel.out_row.MODE_SAME)
    task = panel._make_task(str(sources[0]))
    assert task["output_path"] == str(tmp_path / "one.srt")
    assert task["params"] == panel.collect_params()
    assert task["need_ffmpeg"]
    panel.file_card.clear_files()
    assert "0" in panel.output_hint.text()
    assert not panel.action_bar.btn_go.isEnabled()


def test_subtitle_sections_fit_narrow_window_and_keep_prefs(subtitle_panel):
    app, panel = subtitle_panel
    panel.apply_prefs({"fps": 0.5, "region": "top", "height": "20%", "auto_detect": False})
    expected = panel.collect_prefs()
    panel.show()
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.params_grid._columns == columns
        assert panel.region_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
        for label in (panel.source_hint, panel.sampling_hint, panel.region_hint, panel.output_hint):
            assert label.wordWrap()


def test_subtitle_runner_keeps_auto_and_manual_engine_contract(subtitle_panel, monkeypatch):
    import core.subtitle_extract as engine

    _app, panel = subtitle_panel
    calls = []
    monkeypatch.setattr(engine, "extract_subtitles", lambda *args: calls.append(args) or True)
    # 覆盖自动/手动及全部区域，确保说明调整未改变引擎优先级或单位。
    for auto in (False, True):
        for index, region in enumerate(("bottom", "top", "full")):
            panel.cb_region.setCurrentIndex(index)
            panel.cb_auto.setChecked(auto)
            panel.cb_height.setCurrentText("20%")
            task = SimpleNamespace(file_path="video.mp4", output_path="video.srt", params=panel.collect_params())
            assert panel._runner(task, None)
            assert calls[-1] == ("video.mp4", "video.srt", 1.0, "chi_sim+eng", region, 0.2, auto, None)
