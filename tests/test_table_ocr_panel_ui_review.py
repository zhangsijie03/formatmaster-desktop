"""表格识别页局部体验：适用范围、格式语义与批量输出反馈。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture
def table_panel(monkeypatch):
    from gui_qt.panels.table_ocr_panel import TableOcrPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = TableOcrPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_input_guidance_states_supported_scope(table_panel):
    _app, panel = table_panel
    assert panel.input_hint.wordWrap()
    assert "PDF" in panel.input_hint.text()
    assert "图片" in panel.input_hint.text() or "images" in panel.input_hint.text()


def test_csv_mode_explains_structure_only_output(table_panel):
    _app, panel = table_panel
    panel.cb_fmt.setCurrentIndex(0)
    assert "CSV" in panel.mode_hint.text()
    assert "合并单元格" in panel.mode_hint.text() or "merged cells" in panel.mode_hint.text()
    assert not panel.cb_chart.isEnabled()


def test_excel_chart_feedback_tracks_chart_choice(table_panel):
    _app, panel = table_panel
    panel.cb_fmt.setCurrentIndex(1)
    panel.cb_chart.setCurrentIndex(0)
    assert "工作表" in panel.mode_hint.text() or "worksheet" in panel.mode_hint.text()
    panel.cb_chart.setCurrentIndex(2)
    assert "首行" in panel.mode_hint.text() or "row 1" in panel.mode_hint.text()
    assert panel.cb_chart.currentText() in panel.output_hint.text()
    assert panel.collect_params()["chart_type"] == "line"


def test_batch_output_summary_tracks_count_and_extension(table_panel, tmp_path):
    _app, panel = table_panel
    panel.cb_fmt.setCurrentIndex(0)
    files = [tmp_path / "one.png", tmp_path / "two.jpg"]
    for path in files:
        Image.new("RGB", (30, 20), "white").save(path)
    panel.file_card.add_files([str(path) for path in files])
    assert "2" in panel.output_hint.text()
    assert ".csv" in panel.output_hint.text()
    panel.cb_fmt.setCurrentIndex(1)
    assert ".xlsx" in panel.output_hint.text()


def test_responsive_reflow_preserves_table_settings(table_panel):
    app, panel = table_panel
    panel.cb_fmt.setCurrentIndex(1)
    panel.cb_chart.setCurrentIndex(1)
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
