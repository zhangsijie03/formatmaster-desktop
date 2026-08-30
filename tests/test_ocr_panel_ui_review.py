"""高级 OCR 页局部体验：导出语义、批量结果和预览说明。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture
def ocr_panel(monkeypatch):
    from gui_qt.panels.ocr_panel import OcrPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = OcrPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_txt_mode_explains_plain_text_contract(ocr_panel):
    _app, panel = ocr_panel
    panel.cb_export.setCurrentIndex(0)
    assert "TXT" in panel.mode_hint.text()
    assert "不保留" in panel.mode_hint.text() or "does not preserve" in panel.mode_hint.text()
    assert not panel.sw_table.isEnabled()
    assert not panel.sw_image.isEnabled()


def test_word_mode_explains_pdf_detection_and_options(ocr_panel):
    _app, panel = ocr_panel
    panel.cb_export.setCurrentIndex(1)
    assert "数字 PDF" in panel.mode_hint.text() or "digital PDFs" in panel.mode_hint.text()
    assert "人工复核" in panel.mode_hint.text() or "Review" in panel.mode_hint.text()
    panel.sw_table.setChecked(False)
    panel.sw_image.setChecked(False)
    assert "只输出识别文字" in panel.mode_hint.text() or "text only" in panel.mode_hint.text()


def test_single_and_batch_output_summaries_track_file_count(
        ocr_panel, tmp_path):
    _app, panel = ocr_panel
    files = [tmp_path / "one.png", tmp_path / "two.jpg"]
    for path in files:
        Image.new("RGB", (30, 20), "white").save(path)
    panel.file_card.add_files([str(path) for path in files])
    assert "2" in panel.output_hint.text()
    assert ("只能" in panel.output_hint.text()
            or "single mode" in panel.output_hint.text())
    panel.sw_batch.setChecked(True)
    assert "2" in panel.output_hint.text()
    assert ".txt" in panel.output_hint.text()
    panel.cb_export.setCurrentIndex(1)
    assert ".docx" in panel.output_hint.text()


def test_result_preview_scope_is_explicit(ocr_panel):
    _app, panel = ocr_panel
    assert panel.result_hint.wordWrap()
    assert "Word" in panel.result_hint.text()
    assert "表格" in panel.result_hint.text() or "tables" in panel.result_hint.text()


def test_responsive_reflow_preserves_ocr_settings(ocr_panel):
    app, panel = ocr_panel
    panel.cb_export.setCurrentIndex(1)
    panel.sw_batch.setChecked(False)
    expected = panel.collect_params()
    panel.show()
    for width, columns in ((700, 1), (1120, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.settings_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.collect_params() == expected
        assert panel.mode_hint.wordWrap()
        assert panel.output_hint.wordWrap()
