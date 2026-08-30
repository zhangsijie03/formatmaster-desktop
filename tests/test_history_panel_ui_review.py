"""历史页局部交互回归，记录存储和文件打开使用替身。"""

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

from tests.test_history_review import ConfirmBox, _page, _record


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app, monkeypatch):
    from gui_qt.components import toast

    for name in ("show_info", "show_warning", "show_success", "show_error"):
        monkeypatch.setattr(toast, name, lambda *args, **kwargs: None)
    panel, _ = _page(app, [
        _record("first.mp4", output="/tmp/first.mp4"),
        _record("second.wav", status="failed", type_name="音频转换"),
    ])
    yield panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_reset_restores_all_filters_and_no_results(page):
    from gui_qt.pages.history_page import _RESULTS

    page.search_edit.setText("no-match")
    page.type_combo.setCurrentIndex(1)
    page._on_result_pill(_RESULTS[2])
    assert page.no_results_widget.isVisibleTo(page)
    assert page.btn_reset.isEnabled()
    page.btn_reset.click()
    assert page.search_edit.text() == ""
    assert page.type_combo.currentIndex() == 0
    assert page._result_val == _RESULTS[0]
    assert page.result_pills[0].isChecked()
    assert not any(pill.isChecked() for pill in page.result_pills[1:])
    assert page._model.rowCount() == 2
    assert not page.btn_reset.isEnabled()
    assert not page.no_results_widget.isVisibleTo(page)


def test_selection_count_and_open_availability_follow_multiple_selection(page):
    from gui_qt.i18n import tr

    selection = page.table.selectionModel()
    selection.select(page._model.index(0, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    selection.select(page._model.index(1, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert tr("已选 2 条", "2 selected") in page.count_label.text()
    assert page.btn_delete.isEnabled()
    assert not page.btn_open.isEnabled()
    page.search_edit.setText("first")
    assert "1 / 2" in page.count_label.text()
    assert not page.btn_delete.isEnabled()


@pytest.mark.parametrize("status,path", [("failed", "/tmp/output.mp4"), ("success", ""), ("success", "   ")])
def test_double_click_cannot_open_failed_or_empty_output(page, monkeypatch, status, path):
    monkeypatch.setattr("utils.platform_utils.open_path", lambda path: pytest.fail("unexpected file open"))
    page.services.history.records = [_record("source", status=status, output=path)]
    page._refresh()
    page.table.selectRow(0)
    assert not page.btn_open.isEnabled()
    page.table.doubleClicked.emit(page._model.index(0, 0))


def test_successful_output_opens_exact_path(page, tmp_path, monkeypatch):
    output = tmp_path / "valid output.txt"
    output.touch()
    opened = []
    monkeypatch.setattr("utils.platform_utils.open_path", lambda path: opened.append(path) or True)
    page.services.history.records = [_record("source", output=str(output))]
    page._refresh()
    page.table.selectRow(0)
    page._open_selected()
    assert opened == [str(output)]


def test_delete_blocks_reentry_and_cross_action_while_confirming(page, monkeypatch):
    page.table.selectRow(0)

    class ReentrantBox(ConfirmBox):
        def exec(self):
            assert page._mutating
            assert not page.btn_clear.isEnabled() and not page.btn_delete.isEnabled()
            page._delete_selected()
            page._clear()
            return True

    monkeypatch.setattr("qfluentwidgets.MessageBox", ReentrantBox)
    page._delete_selected()
    history = page.services.history
    assert len(history.deleted) == 1 and history.clear_calls == 0
    assert not page._mutating and page.btn_clear.isEnabled()


def test_cancel_and_storage_failure_restore_actions(page, monkeypatch):
    monkeypatch.setattr("qfluentwidgets.MessageBox", ConfirmBox)
    monkeypatch.setattr(ConfirmBox, "exec", lambda self: False)
    page.table.selectRow(0)
    page._delete_selected()
    assert not page._mutating and page.btn_delete.isEnabled()
    assert len(page.services.history.records) == 2
    monkeypatch.setattr(ConfirmBox, "exec", lambda self: True)

    def fail(records):
        raise OSError("read-only storage")

    monkeypatch.setattr(page.services.history, "delete_records", fail)
    page._delete_selected()
    assert not page._mutating and page.btn_delete.isEnabled()
    assert len(page.services.history.records) == 2


def test_clear_confirmation_covers_full_history_and_resets_filters(page, monkeypatch):
    from gui_qt.pages.history_page import _RESULTS

    messages = []

    class InspectBox(ConfirmBox):
        def __init__(self, title, message, parent):
            super().__init__()
            messages.append(message)

        def exec(self):
            page._clear()
            page._delete_selected()
            return True

    monkeypatch.setattr("qfluentwidgets.MessageBox", InspectBox)
    page.search_edit.setText("no-match")
    page._on_result_pill(_RESULTS[2])
    page._clear()
    assert len(messages) == 1 and "2" in messages[0]
    assert page.services.history.clear_calls == 1
    assert not page.search_edit.text()
    assert page._result_val == _RESULTS[0]
    assert not page._mutating and not page.btn_clear.isEnabled()
    assert page.empty_widget.isVisibleTo(page)
    assert not page.charts_section.isVisibleTo(page)


def test_charts_are_optional_and_statistics_ignore_filters(page):
    assert not page.charts_section.content.isVisibleTo(page)
    page.charts_section.btn.click()
    assert page.chart_trend.isVisibleTo(page)
    page.search_edit.setText("first")
    assert page.stat_total.value_label.text() == "2"
    assert page._model.rowCount() == 1
    page.charts_section.btn.click()
    assert not page.chart_trend.isVisibleTo(page)


def test_newest_first_and_user_sort_survives_refresh(page):
    first, second = page.services.history.records
    first["time"], second["time"] = "2026-08-20 10:00:00", "2026-08-30 10:00:00"
    page._refresh()
    assert page._model.item(0, 2).text() == "second.wav"
    page.table.sortByColumn(0, Qt.AscendingOrder)
    page._refresh()
    assert page._model.item(0, 2).text() == "first.mp4"


@pytest.mark.parametrize("width", [640, 700, 820, 1000, 1100])
@pytest.mark.parametrize("expanded", [False, True])
def test_responsive_layout_and_table_fit(page, app, width, expanded):
    page.charts_section.set_expanded(expanded)
    page.resize(width, 900)
    for _ in range(4):
        app.processEvents()
    assert page.horizontalScrollBar().maximum() == 0
    assert page.table.horizontalScrollBar().maximum() == 0
