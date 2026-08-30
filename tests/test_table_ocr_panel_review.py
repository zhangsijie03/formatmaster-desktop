"""“表格识别”菜单页面与批量任务回归测试。"""

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
    from gui_qt.panels.table_ocr_panel import TableOcrPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = TableOcrPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_page_does_not_require_ffmpeg_and_registers_runner(panel):
    assert panel.need_ffmpeg is False
    assert "table_ocr" in panel.services.task_manager._runner_factories


def test_format_updates_chart_summary_and_stable_preferences(panel, app):
    panel.cb_fmt.setCurrentIndex(0)
    panel.cb_chart.setCurrentIndex(2)
    app.processEvents()
    assert not panel.cb_chart.isEnabled()
    assert "CSV" in panel.file_card._fmt_text
    assert panel.action_bar.btn_go.text() in ("识别并导出 CSV", "OCR to CSV")
    assert panel.collect_prefs() == {
        "output_format": "csv", "chart_type": "line"}

    panel.apply_prefs({"output_format": "xlsx", "chart_type": "pie"})
    assert panel.cb_chart.isEnabled()
    assert "XLSX" in panel.file_card._fmt_text
    assert panel.collect_prefs() == {
        "output_format": "xlsx", "chart_type": "pie"}

    panel.apply_prefs({"fmt": 0, "chart": 1})
    assert panel.cb_fmt.currentIndex() == 0
    assert panel.cb_chart.currentIndex() == 1


def test_csv_task_ignores_disabled_chart_and_uses_stable_params(panel, tmp_path):
    source = tmp_path / "table.png"
    source.write_bytes(b"image")
    panel.cb_fmt.setCurrentIndex(0)
    panel.cb_chart.setCurrentIndex(3)
    panel._reserved_output_paths.clear()

    task = panel._make_task(str(source))

    assert task["params"] == {"output_format": "csv", "chart_type": None}
    assert task["runner_key"] == "table_ocr"
    assert task["output_path"].endswith("table.csv")


def test_same_named_batch_outputs_are_unique(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "table.png"
    second = second_dir / "table.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel.cb_fmt.setCurrentIndex(1)
    panel._reserved_output_paths.clear()

    first_task = panel._make_task(str(first))
    second_task = panel._make_task(str(second))

    assert first_task["output_path"].endswith("table.xlsx")
    assert second_task["output_path"].endswith("table_1.xlsx")


def test_settings_reflow_on_narrow_window(panel, app):
    panel.resize(700, 700)
    panel.show()
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_chart))[0] > 1
    panel.resize(1100, 700)
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_chart))[0] == 1

