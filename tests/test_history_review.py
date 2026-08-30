"""转换历史菜单审查回归：存储事务、筛选映射、记录操作与响应式。"""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QBoxLayout


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance
    instance.processEvents()


class FakeHistory:
    def __init__(self, records):
        self.records = [dict(record) for record in records]
        self.clear_calls = 0
        self.deleted = []

    def get_all(self, limit=None):
        records = self.records[:limit] if limit else self.records
        return [dict(record) for record in records]

    def clear(self):
        self.clear_calls += 1
        self.records = []

    def delete_records(self, records):
        removed = 0
        for record in records:
            if record in self.records:
                self.records.remove(record)
                self.deleted.append(record)
                removed += 1
        return removed


class ConfirmBox:
    def __init__(self, *_args, **_kwargs):
        self.yesButton = SimpleNamespace(setText=lambda _text: None)
        self.cancelButton = SimpleNamespace(setText=lambda _text: None)

    def exec(self):
        return True


def _record(name, status="success", type_name="视频转换", output=""):
    return {
        "time": "2026-08-30 10:20:30", "timestamp": 1,
        "type": type_name, "source": name, "target": "MP4",
        "status": status, "output_path": output, "saved_bytes": 0,
    }


def _page(app, records):
    from gui_qt.pages.history_page import HistoryPage

    history = FakeHistory(records)
    page = HistoryPage(
        SimpleNamespace(pages={}), SimpleNamespace(history=history))
    page.resize(1100, 800)
    page.show()
    app.processEvents()
    return page, history


def test_history_load_rejects_non_list_root(tmp_path, monkeypatch):
    from utils import config

    path = tmp_path / "history.json"
    path.write_text('{"unexpected": true}', encoding="utf-8")
    monkeypatch.setattr(config, "get_history_path", lambda: str(path))

    history = config.ConversionHistory()

    assert history.get_all() == []
    assert not path.exists()
    assert (tmp_path / "history.json.bak").exists()


def test_history_add_copies_input_and_rolls_back_failed_saves(
        tmp_path, monkeypatch):
    from utils import config

    monkeypatch.setattr(
        config, "get_history_path", lambda: str(tmp_path / "history.json"))
    history = config.ConversionHistory()
    source = {"type": "视频转换", "source": "a.mp4", "target": "MP4",
              "status": config.HistoryStatus.SUCCESS}
    history.add(source)

    assert "time" not in source and "timestamp" not in source
    assert history.get_all()[0]["status"] == "success"
    before = history.get_all()
    monkeypatch.setattr(
        history, "_save", lambda: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(OSError):
        history.clear()
    assert history.get_all() == before
    with pytest.raises(OSError):
        history.add({"type": "音频转换", "source": "b.wav",
                     "target": "MP3", "status": "failed"})
    assert history.get_all() == before


def test_delete_records_batches_one_save_and_handles_duplicates(
        tmp_path, monkeypatch):
    from utils import config

    monkeypatch.setattr(
        config, "get_history_path", lambda: str(tmp_path / "history.json"))
    history = config.ConversionHistory()
    history.records = [{"value": 1}, {"value": 1}, {"value": 2}]
    saves = []
    monkeypatch.setattr(history, "_save", lambda: saves.append(True))

    assert history.delete_records([{"value": 1}, {"value": 1}]) == 2
    assert history.get_all() == [{"value": 2}]
    assert saves == [True]


def test_task_history_uses_name_for_downloads_and_reports_save_failure(
        app, monkeypatch, tmp_path):
    from gui_qt.task_manager import Task, TaskManager
    from utils.config import HistoryStatus

    monkeypatch.setattr(TaskManager, "_load_snapshot", lambda _self: None)

    class Sink:
        def __init__(self):
            self.items = []

        def add(self, item):
            self.items.append(item)

    sink = Sink()
    services = SimpleNamespace(
        history=sink, get_pref=lambda _key, default=None: default)
    manager = TaskManager(services)
    manager._dispatch_timer.stop()
    task = Task(
        task_id=1, name="M3U8下载 - lesson", task_type="m3u8",
        file_path="", output_path=str(tmp_path / "lesson.mp4"),
        history_type="M3U8 下载", history_target="M3U8下载")
    manager._record_history(task, True)

    assert sink.items[0]["source"] == "M3U8下载 - lesson"
    assert sink.items[0]["status"] is HistoryStatus.SUCCESS

    logs = []
    manager.sig_log.connect(lambda message, level: logs.append((message, level)))
    services.history = SimpleNamespace(
        add=lambda _item: (_ for _ in ()).throw(OSError("denied")))
    manager._record_history(task, False)
    app.processEvents()
    assert logs and logs[-1][1] == "warning" and "denied" in logs[-1][0]
    manager._snapshot_timer.stop()
    manager._executor.shutdown(wait=True)


def test_cross_language_types_share_one_filter(app):
    page, _history = _page(app, [
        _record("cn.mp4", type_name="视频转换",
                output="/tmp/export-name.mp4"),
        _record("en.mp4", type_name="Video Convert"),
    ])
    try:
        assert page.type_combo.count() == 2
        assert page.type_combo.itemData(1) == "视频转换"
        page.search_edit.setText("export-name")
        app.processEvents()
        assert page._model.rowCount() == 1
        page.search_edit.clear()
        page.type_combo.setCurrentIndex(1)
        app.processEvents()
        assert page._model.rowCount() == 2
    finally:
        page.close()
        page.deleteLater()


def test_clear_still_works_when_filter_has_no_matches(app, monkeypatch):
    page, history = _page(app, [_record("source.mp4")])
    monkeypatch.setattr("qfluentwidgets.MessageBox", ConfirmBox)
    try:
        page.search_edit.setText("no-such-record")
        app.processEvents()
        assert page._model.rowCount() == 0
        assert page.no_results_widget.isVisibleTo(page)
        page._clear()
        assert history.clear_calls == 1
        assert history.records == []
    finally:
        page.close()
        page.deleteLater()


def test_sorted_selection_deletes_attached_record(app, monkeypatch):
    page, history = _page(app, [
        _record("first.mp4"), _record("second.mp4", status="failed")])
    monkeypatch.setattr("qfluentwidgets.MessageBox", ConfirmBox)
    try:
        page.table.sortByColumn(2, Qt.DescendingOrder)
        page.table.selectRow(0)
        app.processEvents()
        selected = page._selected_records()
        assert len(selected) == 1
        page._delete_selected()
        assert history.deleted == selected
        assert len(history.records) == 1
    finally:
        page.close()
        page.deleteLater()


def test_missing_output_explains_next_step(app, monkeypatch, tmp_path):
    missing = tmp_path / "missing.mp4"
    page, _history = _page(
        app, [_record("source.mp4", output=str(missing))])
    warnings = []
    monkeypatch.setattr(
        "gui_qt.pages.history_page.toast.show_warning",
        lambda _parent, message: warnings.append(message))
    try:
        page.table.selectRow(0)
        app.processEvents()
        assert page.btn_open.isEnabled()
        page._open_selected()
        assert warnings and "保存目录" in warnings[-1]
    finally:
        page.close()
        page.deleteLater()


def test_narrow_history_layout_has_no_horizontal_scroll(app):
    page, _history = _page(app, [
        _record("source-" + "x" * 120 + ".mp4",
                output="/tmp/" + "y" * 120 + ".mp4"),
        _record("failed.mkv", status="failed", type_name="M3U8 Download"),
    ])
    try:
        page.resize(700, 900)
        page.show()
        app.processEvents()
        assert page.charts_layout.direction() == QBoxLayout.TopToBottom
        assert page.toolbar_layout.direction() == QBoxLayout.TopToBottom
        assert page.horizontalScrollBar().maximum() == 0
        assert page.table.horizontalScrollBar().maximum() == 0
        assert page.stats_layout.itemAtPosition(1, 0).widget() is page.stat_fail
    finally:
        page.close()
        page.deleteLater()
