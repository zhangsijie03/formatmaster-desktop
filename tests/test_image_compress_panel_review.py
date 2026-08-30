"""“图片压缩”菜单页面与任务参数回归测试。"""

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
    from gui_qt.panels.compress_img_panel import CompressImgPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = CompressImgPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_mode_updates_controls_action_and_summary(panel, app):
    assert panel.need_ffmpeg is False
    panel.seg_mode.setCurrentItem("target")
    app.processEvents()
    assert panel._mode == "target"
    assert panel._target_row.isVisibleTo(panel)
    assert not panel.cb_q.isEnabled()
    assert not panel.cb_sz.isEnabled()
    assert panel.action_bar.btn_go.text() in (
        "压缩至目标大小", "Compress to target")
    assert "500" in panel.file_card._fmt_text

    panel.seg_mode.setCurrentItem("quality")
    app.processEvents()
    assert panel.cb_q.isEnabled()
    assert panel.cb_sz.isEnabled()
    assert "75" in panel.file_card._fmt_text


def test_preferences_restore_integer_target_and_stable_size(panel):
    panel.apply_prefs({
        "quality": "60", "size_index": 2,
        "mode": "target", "target_kb": 1024,
    })
    assert panel._mode == "target"
    assert panel.cb_target.currentText() == "1024"
    assert panel.cb_sz.currentIndex() == 2

    panel.seg_mode.setCurrentItem("quality")
    params = panel.collect_params()
    assert params["quality"] == 60
    assert params["max_size"] == (1280, 720)
    assert params["target_kb"] is None


def test_same_named_batch_outputs_are_unique_and_recoverable(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "photo.png"
    second = second_dir / "photo.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel.seg_mode.setCurrentItem("quality")
    panel._reserved_output_paths.clear()

    first_task = panel._make_task(str(first))
    second_task = panel._make_task(str(second))

    assert first_task["output_path"] != second_task["output_path"]
    assert first_task["output_path"].endswith("photo_compressed.png")
    assert second_task["output_path"].endswith("photo_compressed_1.png")
    assert first_task["runner_key"] == "compress_img"
    assert "compress_img" in panel.services.task_manager._runner_factories
