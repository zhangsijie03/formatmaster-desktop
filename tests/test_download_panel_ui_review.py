"""视频下载页面交互回归；只模拟格式和任务，不访问外部网站。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app, monkeypatch):
    from gui_qt.components import toast
    from gui_qt.panels.download_panel import DownloadPanelPage
    from gui_qt.services import QtServices
    from tests.test_video_download_review import FakeTaskManager

    services = QtServices()
    services.task_manager = FakeTaskManager()
    monkeypatch.setattr(services, "get_pref", lambda key, default=None: default)
    monkeypatch.setattr(services, "set_pref", lambda *args: None)
    for name in ("show_info", "show_warning", "show_success", "show_error"):
        monkeypatch.setattr(toast, name, lambda *args, **kwargs: None)
    panel = DownloadPanelPage(object(), services)
    yield panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_parsing_multiple_links_preserves_input_and_never_starts_worker(page, monkeypatch):
    from gui_qt.panels import download_panel

    raw = "https://example.com/a\nhttps://example.com/b"
    page.txt_url.setPlainText(raw)
    monkeypatch.setattr(download_panel, "_ParseWorker", lambda *args: pytest.fail("unexpected parse"))
    page._parse_url()
    assert page.txt_url.toPlainText() == raw
    assert page._worker is None
    page._add_url()
    assert [entry["url"] for entry in page._queue] == raw.splitlines()


def test_collapsed_settings_summary_tracks_audio_and_extras(page):
    from gui_qt.i18n import tr

    page.cb_audio_only.setChecked(True)
    page.cb_audio_fmt.setCurrentText("flac")
    page.cb_subtitles.setChecked(True)
    page.cb_thumb.setChecked(True)
    page.cb_speed.setCurrentText("5")
    summary = page.lb_settings_summary.text()
    assert "FLAC" in summary and "5 MB/s" in summary
    assert tr("字幕", "subtitles") in summary
    assert tr("封面", "thumbnail") in summary
    page.cb_video_only.setChecked(True)
    assert tr("无声音", "no audio") in page.lb_settings_summary.text()
    assert not page.cb_audio_only.isChecked()


def test_queue_retains_specific_format_and_full_url(page):
    url = "https://example.com/" + "long-path-" * 20
    page.txt_url.setPlainText(url)
    page._on_formats([{"format_id": "137", "ext": "mp4", "resolution": "1080p"}],
                     "Example", None, url)
    page.lst_formats.setCurrentRow(0)
    page._add_url()
    item = page.lst_queue.item(0)
    assert page._queue[0]["fmt_id"] == "137"
    assert "137" in item.text()
    assert url in item.toolTip() and "137" in item.toolTip()


def test_queue_controls_follow_selection_and_order(page):
    page.txt_url.setPlainText("https://example.com/a\nhttps://example.com/b")
    page._add_url()
    page.lst_queue.setCurrentRow(-1)
    assert not page.btn_remove.isEnabled()
    page.lst_queue.setCurrentRow(0)
    assert not page.btn_up.isEnabled() and page.btn_down.isEnabled()
    page._move(1)
    assert page._queue[1]["url"].endswith("/a")
    assert page.btn_up.isEnabled() and not page.btn_down.isEnabled()
    page._remove_selected()
    assert len(page._queue) == 1
    assert not page.btn_up.isEnabled() and not page.btn_down.isEnabled()


def test_duplicate_submission_and_reentry_are_blocked(page, tmp_path, monkeypatch):
    manager = page.services.task_manager
    original = manager.add_task
    page.ed_dir.setText(str(tmp_path))
    page.txt_url.setPlainText("https://example.com/a")
    page._add_url()

    def add_task(**kwargs):
        assert not page.action_bar.btn_go.isEnabled()
        page._start()
        return original(**kwargs)

    monkeypatch.setattr(manager, "add_task", add_task)
    page._start()
    page._start()
    assert len(manager.tasks) == 1
    assert page._task_rows
    assert not page._submitting
    page.lst_queue.setCurrentRow(0)
    page._remove_selected()
    assert len(page._queue) == 1


def test_invalid_start_restores_submission_state(page):
    page.txt_url.setPlainText("https://example.com/a")
    page._add_url()
    page.ed_dir.clear()
    page._start()
    assert not page._submitting
    assert page.action_bar.btn_go.isEnabled()
    assert not page.services.task_manager.tasks


@pytest.mark.parametrize("width", [640, 700, 1100])
def test_queue_and_long_parse_error_fit_window(page, app, width):
    page.txt_url.setPlainText("https://example.com/a")
    page._add_url()
    page._on_parse_fail("Remote error " * 50)
    page.resize(width, 900)
    page.show()
    for _ in range(3):
        app.processEvents()
    assert page.horizontalScrollBar().maximum() == 0
    assert page.lb_fmt_info.wordWrap()
    assert page.lb_fmt_info.toolTip() == ("Remote error " * 50).strip()
