"""水印页局部体验：尺寸语义、批量输出与处理契约。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture
def watermark_panel(monkeypatch):
    from gui_qt.panels.watermark_panel import WatermarkPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = WatermarkPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"wm_type": "text", "text": "Watermark",
                       "font_size": "48", "color": "#FFFFFF",
                       "opacity": "0.8", "rotation": "0",
                       "position": "bottom_right"})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_text_watermark_summary_tracks_visible_parameters(watermark_panel):
    _app, panel = watermark_panel
    panel.ed_text.setText("品牌水印示例文字非常非常长")
    panel.cb_font_size.setCurrentText("64")
    panel.cb_color.setCurrentText("#FF0000")
    panel.cb_opacity.setCurrentText("0.5")
    panel.cb_rotation.setCurrentText("15")
    panel.cb_position.setCurrentIndex(0)
    assert "64" in panel.mode_hint.text()
    assert "#FF0000" in panel.mode_hint.text()
    assert "50" in panel.mode_hint.text()
    assert "15" in panel.mode_hint.text()
    assert panel.cb_position.currentText() in panel.mode_hint.text()
    assert "…" in panel.file_card._fmt_text
    params = panel.collect_params()
    assert params["wm_type"] == "text"
    assert params["opacity"] == 0.5
    assert params["rotation"] == 15


def test_image_watermark_summary_explains_source_width_scale(
        watermark_panel, tmp_path):
    _app, panel = watermark_panel
    watermark = tmp_path / "logo.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 128)).save(watermark)
    panel.rb_image.setChecked(True)
    panel.ed_wm_path.setText(str(watermark))
    panel.cb_scale.setCurrentText("0.3")
    panel.cb_opacity_img.setCurrentText("0.7")
    panel.cb_rotation_img.setCurrentText("30")
    assert "logo.png" in panel.mode_hint.text()
    assert "30%" in panel.mode_hint.text()
    assert "70%" in panel.mode_hint.text()
    assert "30°" in panel.mode_hint.text()
    assert panel.ed_wm_path.toolTip() == str(watermark)
    params = panel.collect_params()
    assert params["wm_type"] == "image"
    assert params["scale"] == 0.3


def test_watermark_batch_count_and_output_contract(watermark_panel, tmp_path):
    _app, panel = watermark_panel
    sources = [tmp_path / "one.png", tmp_path / "two.jpg"]
    for source in sources:
        Image.new("RGB", (20, 20), "white").save(source)
    panel.file_card.add_files([str(source) for source in sources])
    assert "2" in panel.output_hint.text()
    panel.out_row.resolve_dir = lambda _path: str(tmp_path)
    panel._reserved_output_paths = set()
    tasks = [panel._make_task(str(source)) for source in sources]
    assert tasks[0]["output_path"].endswith("one_watermark.png")
    assert tasks[1]["output_path"].endswith("two_watermark.jpg")
    assert all(not task["need_ffmpeg"] for task in tasks)
    panel.file_card.clear_files()
    assert "0" in panel.output_hint.text()


def test_watermark_runner_keeps_existing_engine_contract(watermark_panel, monkeypatch):
    import gui_qt.panels.watermark_panel as panel_module

    _app, panel = watermark_panel
    calls = []
    monkeypatch.setattr(panel_module, "process_watermark",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    params = {
        "wm_type": "image", "text": "ignored", "font_size": 64,
        "color": "#FF0000", "opacity": 0.7, "rotation": 30,
        "position": "top_right", "wm_image_path": "logo.png", "scale": 0.3,
    }
    task = SimpleNamespace(file_path="in.png", output_path="out.png", params=params)
    assert panel._runner(task, None)
    assert calls[-1] == (("in.png", "out.png"), {
        "wm_type": "image", "text": "ignored", "font_size": 64,
        "color": "#FF0000", "opacity": 0.7, "rotation": 30,
        "position": "top_right", "wm_image_path": "logo.png",
        "scale": 0.3, "progress_cb": None})


def test_watermark_forms_reflow_without_changing_preferences(watermark_panel):
    app, panel = watermark_panel
    expected = panel.collect_prefs()
    panel.show()
    for width, columns in ((720, 1), (1280, 3)):
        panel.resize(width, 1000)
        app.processEvents()
        assert panel.text_grid._columns == columns
        assert panel.image_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
        assert panel.mode_hint.wordWrap()
        assert panel.output_hint.wordWrap()
