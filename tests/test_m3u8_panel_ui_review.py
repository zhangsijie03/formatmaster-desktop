"""M3U8 页面交互回归；解析和提交均使用替身，不访问外部网络。"""

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app, monkeypatch):
    from gui_qt.components import toast
    from gui_qt.panels.m3u8_panel import M3u8PanelPage
    from gui_qt.services import QtServices
    from tests.test_m3u8_review import FakeTaskManager

    services = QtServices()
    services.task_manager = FakeTaskManager()
    monkeypatch.setattr(services, "get_pref", lambda key, default=None: default)
    monkeypatch.setattr(services, "set_pref", lambda *args: None)
    monkeypatch.setattr(services.prefs, "get", lambda panel, key, default=None: default)
    monkeypatch.setattr(services.prefs, "set_now", lambda *args: None)
    monkeypatch.setattr(services, "ffmpeg_ready", lambda: True)
    for name in ("show_info", "show_warning", "show_success", "show_error"):
        monkeypatch.setattr(toast, name, lambda *args, **kwargs: None)
    panel = M3u8PanelPage(object(), services)
    monkeypatch.setattr(panel, "_parse_url_for", lambda url: None)
    yield panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def qualities():
    return [{"url": f"https://example.com/{label}.m3u8", "label": label,
             "display": label} for label in ("1080p", "720p")]


def test_single_add_keeps_source_and_quality_changes_queued_task(page):
    url = "https://example.com/master.m3u8"
    page.txt_url.setPlainText(url)
    page._batch_add()
    assert page.txt_url.toPlainText() == url
    page._on_qualities(qualities(), [], url)
    page.cb_quality.setCurrentIndex(1)
    assert page._queue[0]["url"].endswith("/720p.m3u8")
    assert "720p" in page.lst_queue.item(0).text()
    assert url in page.lst_queue.item(0).toolTip()
    page._batch_add()
    assert len(page._queue) == 1


def test_multi_link_parse_preserves_input_and_never_parses_first_only(page, monkeypatch):
    raw = "https://example.com/a.m3u8\nhttps://example.com/b.m3u8"
    page.txt_url.setPlainText(raw)
    monkeypatch.setattr(page, "_parse_url_for", lambda url: pytest.fail("unexpected parse"))
    page._parse_url()
    assert page.txt_url.toPlainText() == raw
    page._batch_add()
    assert len(page._queue) == 2
    assert not page.txt_url.toPlainText()


def test_old_parse_results_cannot_replace_new_link(page):
    from gui_qt.panels.m3u8_panel import AUTO_QUALITY

    old = "https://example.com/old.m3u8"
    page.txt_url.setPlainText("https://example.com/new.m3u8")
    page._on_qualities(qualities(), [], old)
    page._on_parse_fail("old error", old)
    assert not page._qualities
    assert not page._quality_source_url
    assert page.cb_quality.currentText() == AUTO_QUALITY
    assert not page.cb_quality.isEnabled()


def test_settings_summary_tracks_options(page):
    from gui_qt.i18n import tr

    page.cb_resume.setChecked(False)
    page.cb_download_sub.setChecked(True)
    page.cb_speed.setCurrentText("5")
    text = page.lb_settings_summary.text()
    assert "5 MB/s" in text
    assert tr("断点续传关闭", "resume off") in text
    assert tr("同时下载字幕", "with subtitles") in text


def test_queue_selection_order_and_full_capacity(page, monkeypatch):
    from gui_qt.panels import m3u8_panel

    assert not page.action_bar.btn_go.isEnabled()
    page.txt_url.setPlainText("https://example.com/a\nhttps://example.com/b")
    page._batch_add()
    page.lst_queue.setCurrentRow(0)
    assert not page.btn_up.isEnabled() and page.btn_down.isEnabled()
    page._move(1)
    assert page.btn_up.isEnabled() and not page.btn_down.isEnabled()
    monkeypatch.setattr(m3u8_panel, "MAX_QUEUE_ITEMS", 2)
    page.txt_url.setPlainText("https://example.com/c")
    page._batch_add()
    assert len(page._queue) == 2
    assert page.txt_url.toPlainText().endswith("/c")
    page._remove_selected()
    assert len(page._queue) == 1
    assert not page.btn_up.isEnabled() and not page.btn_down.isEnabled()


def test_submission_reentry_and_active_queue_mutations_blocked(page, tmp_path, monkeypatch):
    page.txt_url.setPlainText("https://example.com/master.m3u8")
    page._batch_add()
    page._on_qualities(qualities(), [], page.txt_url.toPlainText())
    page.ed_dir.setText(str(tmp_path))
    manager = page.services.task_manager
    original = manager.add_task

    def add_task(**kwargs):
        assert not page.action_bar.btn_go.isEnabled()
        page._start()
        return original(**kwargs)

    monkeypatch.setattr(manager, "add_task", add_task)
    page._start()
    page._start()
    assert len(manager.tasks) == 1
    assert not page._submitting and not page.cb_quality.isEnabled()
    page.lst_queue.setCurrentRow(0)
    page._remove_selected()
    page._clear_queue()
    page.cb_quality.setCurrentIndex(1)
    assert len(page._queue) == 1
    assert page._queue[0]["url"].endswith("/1080p.m3u8")


def test_invalid_start_restores_button(page):
    page.txt_url.setPlainText("https://example.com/master.m3u8")
    page._batch_add()
    page.ed_dir.clear()
    page._start()
    assert not page._submitting
    assert page.action_bar.btn_go.isEnabled()


def test_clear_requires_confirmation(page, monkeypatch):
    from qfluentwidgets import MessageBox

    page.txt_url.setPlainText("https://example.com/master.m3u8")
    page._batch_add()
    monkeypatch.setattr(MessageBox, "exec", lambda self: 0)
    page._clear_queue()
    assert len(page._queue) == 1
    monkeypatch.setattr(MessageBox, "exec", lambda self: 1)
    page._clear_queue()
    assert not page._queue and not page.action_bar.btn_go.isEnabled()


@pytest.mark.parametrize("width", [640, 700, 820, 1000, 1100])
def test_long_parse_error_and_advanced_options_fit(page, app, width):
    page._on_parse_fail("<network> " + "long failure detail " * 30)
    page.advanced_section.set_expanded(True)
    page.resize(width, 1000)
    page.show()
    for _ in range(4):
        app.processEvents()
    assert page.lb_quality_hint.wordWrap()
    assert page.horizontalScrollBar().maximum() == 0
