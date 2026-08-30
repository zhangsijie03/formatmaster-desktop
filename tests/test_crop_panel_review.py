"""封面裁剪局部交互：预览不写原图，模式一致，批量提交防重。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication


@pytest.fixture
def crop_panel(tmp_path, monkeypatch):
    from gui_qt.panels.crop_panel import CropPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = CropPanelPage(object(), services)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    panel.cb_preset.setCurrentIndex(0)
    panel.cb_mode.setCurrentIndex(0)
    source = tmp_path / "landscape.png"
    image = QImage(300, 100, QImage.Format_RGB32)
    image.fill(QColor("green"))
    painter = QPainter(image)
    painter.fillRect(0, 0, 100, 100, QColor("red"))
    painter.fillRect(200, 0, 100, 100, QColor("blue"))
    painter.end()
    assert image.save(str(source))
    yield app, panel, source
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_crop_preview_and_modes_preserve_source(crop_panel):
    from gui_qt.panels.crop_panel import CROP_COVER, CROP_FIT

    app, panel, source = crop_panel
    before = source.read_bytes()
    assert not panel.btn_preview.isEnabled()
    panel.file_card.add_files([str(source)])
    assert panel.btn_preview.isEnabled()
    panel._preview_crop()
    assert panel._preview_path == str(source)
    assert panel.preview_hint.toolTip() == str(source)
    assert not panel.aspect_preview._source.isNull()
    assert max(panel.aspect_preview._source.width(), panel.aspect_preview._source.height()) <= 640
    preview = panel.aspect_preview
    preview.resize(300, 200)
    preview.show()
    app.processEvents()
    assert panel.collect_params()["crop_mode"] == CROP_COVER
    rendered = preview.grab().toImage()
    assert rendered.pixelColor(rendered.width() // 2, 25) == QColor("green")
    panel.cb_mode.setCurrentIndex(1)
    assert panel.collect_params()["crop_mode"] == CROP_FIT
    rendered = preview.grab().toImage()
    assert rendered.pixelColor(rendered.width() // 2, 25) == QColor("white")
    assert panel._preview_path == str(source)
    assert source.read_bytes() == before


def test_crop_selection_clears_stale_preview_and_batch_summary(crop_panel, tmp_path):
    app, panel, source = crop_panel
    second = tmp_path / "portrait.png"
    image = QImage(100, 300, QImage.Format_RGB32)
    image.fill(QColor("blue"))
    assert image.save(str(second))
    panel.file_card.add_files([str(source), str(second)])
    panel._preview_crop()
    panel.file_card.table.selectRow(1)
    assert panel.aspect_preview._source.isNull()
    panel._preview_crop()
    assert panel._preview_path == str(second)
    assert panel.aspect_preview._source.height() > panel.aspect_preview._source.width()
    panel.cb_preset.setCurrentIndex(3)
    assert "1080×1920" in panel.output_hint.text()
    assert "2" in panel.output_hint.text()
    assert panel.aspect_preview._size == (1080, 1920)
    panel.file_card.clear_files()
    assert panel.aspect_preview._source.isNull()
    assert not panel.btn_preview.isEnabled()
    assert not panel.action_bar.btn_go.isEnabled()
    assert "0" in panel.output_hint.text()


def test_crop_preview_exif_orientation(crop_panel, tmp_path):
    from PIL import Image

    app, panel, _source = crop_panel
    source = tmp_path / "phone.jpg"
    with Image.new("RGB", (120, 60), "red") as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(source, exif=exif)
    panel.file_card.add_files([str(source)])
    panel._preview_crop()
    assert panel.aspect_preview._source.height() > panel.aspect_preview._source.width()


def test_crop_preview_rejects_oversized_and_corrupt_images(crop_panel, tmp_path, monkeypatch):
    import gui_qt.panels.crop_panel as module

    app, panel, source = crop_panel
    errors = []
    monkeypatch.setattr(module.toast, "show_warning", lambda _parent, text: errors.append(text))
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    panel.file_card.add_files([str(bad)])
    panel._preview_crop()
    assert errors
    assert panel.aspect_preview._source.isNull()

    class OversizedReader:
        def __init__(self, path):
            pass

        def setAutoTransform(self, enabled):
            pass

        def size(self):
            return QSize(module.PREVIEW_MAX_PIXELS + 1, 1)

        def read(self):
            pytest.fail("Oversized images must not be decoded for preview")

    monkeypatch.setattr(module, "QImageReader", OversizedReader)
    panel._preview_crop()
    assert len(errors) == 2
    assert panel.action_bar.btn_go.isEnabled()


def test_crop_responsive_layout_and_preferences(crop_panel):
    from gui_qt.panels.crop_panel import MODE_VALUES, CROP_FIT

    app, panel, source = crop_panel
    panel.apply_prefs({"preset": panel._preset_items[3][0], "mode": MODE_VALUES[1]})
    panel.show()
    for width, columns in ((720, 1), (1280, 2), (720, 1)):
        panel.resize(width, 1100)
        app.processEvents()
        assert panel.params_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_params()["crop_mode"] == CROP_FIT
        assert panel.collect_params()["preset_size"] == [1080, 1920]


def test_crop_submission_keeps_batch_contract_and_prevents_duplicate(crop_panel, tmp_path, monkeypatch):
    from gui_qt import task_manager as tm

    app, panel, source = crop_panel
    panel.file_card.add_files([str(source)])
    panel.out_row.resolve_dir = lambda path: str(tmp_path / "output")
    calls = []

    def add_task(**kwargs):
        calls.append(kwargs)
        return 42

    monkeypatch.setattr(panel.services.task_manager, "add_task", add_task)
    monkeypatch.setattr(panel.services.task_manager, "get_task",
                        lambda task_id: SimpleNamespace(state=tm.WAITING))
    assert panel._start() is True
    assert panel._start() is False
    assert len(calls) == 1
    assert calls[0]["params"]["files"] == [str(source)]
    assert calls[0]["params"]["preset_size"] == [1080, 1080]
    assert calls[0]["need_ffmpeg"] is False
    assert calls[0]["task_type"] == "crop"
