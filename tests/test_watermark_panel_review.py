"""“水印处理”菜单页面与任务参数定向回归测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.watermark_panel import WatermarkPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = WatermarkPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_modes_update_action_visibility_and_summary(panel, app):
    assert panel.need_ffmpeg is False
    panel.rb_text.setChecked(True)
    panel.cb_position.setCurrentIndex(0)
    app.processEvents()
    assert panel.sec_text.isVisibleTo(panel)
    assert not panel.sec_image.isVisibleTo(panel)
    assert panel.action_bar.btn_go.text() in (
        "添加文字水印", "Add text watermark")
    assert panel.collect_params()["position"] == "top_left"
    assert panel.cb_position.currentText() in panel.file_card._fmt_text

    panel.rb_image.setChecked(True)
    panel.cb_position_img.setCurrentIndex(4)
    app.processEvents()
    assert panel.sec_image.isVisibleTo(panel)
    assert panel.collect_params()["position"] == "center"
    assert panel.action_bar.btn_go.text() in (
        "添加图片水印", "Add image watermark")


def test_image_preferences_restore_completely(panel, tmp_path):
    watermark = tmp_path / "watermark.png"
    Image.new("RGBA", (20, 10), (255, 0, 0, 128)).save(watermark)
    panel.apply_prefs({
        "wm_type": "image", "text": "品牌",
        "font_size": "64", "color": "#FF0000",
        "opacity": "0.5", "rotation": "15", "position": "top_right",
        "scale": "0.3", "opacity_img": "0.7",
        "rotation_img": "90", "position_img": "bottom_left",
        "wm_image_path": str(watermark),
    })
    assert panel.rb_image.isChecked()
    assert panel.ed_wm_path.text() == str(watermark)
    assert panel.cb_scale.currentText() == "0.3"
    assert panel.cb_opacity_img.currentText() == "0.7"
    assert panel.cb_rotation_img.currentText() == "90"
    assert panel.collect_params()["position"] == "bottom_left"
    prefs = panel.collect_prefs()
    assert prefs["position"] == "top_right"
    assert prefs["position_img"] == "bottom_left"


def test_same_named_batch_outputs_are_unique_and_recoverable(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "photo.png"
    second = second_dir / "photo.png"
    Image.new("RGB", (20, 20), "white").save(first)
    Image.new("RGB", (20, 20), "black").save(second)
    panel.rb_text.setChecked(True)
    panel.ed_text.setText("WM")
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel._reserved_output_paths.clear()

    first_task = panel._make_task(str(first))
    second_task = panel._make_task(str(second))

    assert first_task["output_path"] != second_task["output_path"]
    assert first_task["output_path"].endswith("photo_watermark.png")
    assert second_task["output_path"].endswith("photo_watermark_1.png")
    assert first_task["runner_key"] == "watermark"
    assert "watermark" in panel.services.task_manager._runner_factories
