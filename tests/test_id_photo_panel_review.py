"""“证件照换底色”菜单页面与任务链路定向回归测试。"""

import os
from types import SimpleNamespace

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
    from gui_qt.panels.id_photo_panel import IdPhotoPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = IdPhotoPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_page_does_not_require_ffmpeg_and_uses_stable_keys(panel):
    assert panel.need_ffmpeg is False
    panel.cb_bg.setCurrentIndex(6)
    panel.cb_size.setCurrentIndex(2)
    params = panel.collect_params()
    assert params["bg"] == "透明PNG"
    assert params["size_key"] == "1寸"


def test_workspace_reflows_on_narrow_window(panel, app):
    from PySide6.QtWidgets import QBoxLayout

    panel.resize(700, 700)
    panel.show()
    app.processEvents()
    assert panel.workspace_lay.direction() == QBoxLayout.Direction.TopToBottom
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_bg))[0] == 3
    panel.resize(1100, 700)
    app.processEvents()
    assert panel.workspace_lay.direction() == QBoxLayout.Direction.LeftToRight
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_bg))[0] == 1


def test_preview_overlays_do_not_modify_clean_output(tmp_path):
    from gui_qt.panels.id_photo_panel import _PreviewWorker

    source = tmp_path / "source.png"
    clean = tmp_path / "clean.png"
    display = tmp_path / "display.png"
    Image.new("RGB", (60, 80), "white").save(source)
    result = []
    worker = _PreviewWorker(
        str(source), str(clean), "透明PNG", False,
        size=(40, 50), show_guides=True, display_out=str(display))
    worker.sig_done.connect(lambda ok, path: result.append((ok, path)))
    worker.work()

    assert result == [(True, str(display))]
    with Image.open(clean) as clean_image, Image.open(display) as display_image:
        assert clean_image.mode == "RGBA"
        assert display_image.mode == "RGB"


def test_same_named_batch_outputs_are_unique(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "photo.jpg"
    second = second_dir / "photo.jpg"
    Image.new("RGB", (20, 20), "white").save(first)
    Image.new("RGB", (20, 20), "black").save(second)
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel.cb_bg.setCurrentIndex(0)
    panel.cb_named.setChecked(False)
    panel._reserved_output_paths.clear()

    first_task = panel._make_task(str(first))
    second_task = panel._make_task(str(second))

    assert first_task["output_path"].endswith("photo.jpg")
    assert second_task["output_path"].endswith("photo_1.jpg")
    assert first_task["runner_key"] == "id_photo"
    assert "id_photo" in panel.services.task_manager._runner_factories


def test_transparent_task_forces_png(panel, tmp_path):
    source = tmp_path / "portrait.jpg"
    Image.new("RGB", (20, 20), "white").save(source)
    panel.cb_bg.setCurrentIndex(6)
    panel._reserved_output_paths.clear()
    task = panel._make_task(str(source))
    assert task["output_path"].endswith(".png")


def test_custom_dimension_preferences_keep_original_unit(panel):
    panel.apply_prefs({
        "size_key": "自定义", "custom_width": "2.5",
        "custom_height": "3.5", "custom_unit": "cm",
        "layout_paper": "5寸", "bg": ["broken", 0, 0],
    })
    params = panel.collect_params()
    assert params["custom_unit"] == "cm"
    assert params["custom_width"] == "2.5"
    assert params["custom_height"] == "3.5"
    assert params["custom_size"] == (295, 413)
    assert params["layout_paper"] == "5寸"


def test_invalid_custom_size_blocks_submission(panel, monkeypatch):
    panel.cb_size.setCurrentIndex(12)
    panel.le_cw.setText("")
    panel.le_ch.setText("20")
    called = []
    monkeypatch.setattr(panel, "_submit_files", lambda: called.append(True))
    assert panel._start() is False
    assert not called


def test_print_layout_failure_preserves_existing_output(panel, monkeypatch, tmp_path):
    source = tmp_path / "source.jpg"
    output = tmp_path / "output.jpg"
    Image.new("RGB", (60, 80), "white").save(source)
    output.write_bytes(b"old-result")
    task = SimpleNamespace(
        file_path=str(source), output_path=str(output),
        params={"bg": "白底", "use_ai": False, "size_key": "1寸",
                "custom_size": None, "offset": 0.0, "dpi": 300,
                "quality": 95, "do_print": True, "paper": "6寸"})
    monkeypatch.setattr(
        "gui_qt.panels.id_photo_panel.layout_print",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad layout")))
    messages = []
    assert panel._runner(task, lambda pct, msg: messages.append((pct, msg))) is False
    assert output.read_bytes() == b"old-result"
    assert any(pct < 0 and "bad layout" in msg for pct, msg in messages)
    assert not any(item.name.startswith(".fm_idphoto_")
                   for item in tmp_path.iterdir())
