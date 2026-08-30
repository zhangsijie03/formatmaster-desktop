"""视频下载菜单审查回归：网络参数、输出边界、并发隔离和页面状态。"""

import os
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QBoxLayout


@pytest.fixture(scope="module")
def app_ctx():
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.services import QtServices

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.theme_mgr = ThemeManager(services)

    class Window:
        pass

    yield app, Window(), services
    app.processEvents()


class FakeTaskManager(QObject):
    sig_progress = Signal(int, int, str, str)
    sig_state = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.tasks = []

    def all_tasks(self):
        return []

    def add_task(self, **kwargs):
        task_id = len(self.tasks) + 1
        self.tasks.append(SimpleNamespace(task_id=task_id, **kwargs))
        return task_id

    def cancel_task(self, _task_id):
        pass

    def get_task(self, _task_id):
        return None


def test_url_cleaning_rejects_plain_text_and_invalid_ports():
    from gui_qt.panels.download_panel import _clean_url, _extract_urls

    assert _clean_url("not-a-url") == ""
    assert _clean_url("file:///tmp/video.mp4") == ""
    assert _clean_url("https://example.com:99999/video") == ""
    assert _clean_url("分享 https://example.com/watch?v=1。") == \
        "https://example.com/watch?v=1"
    assert _extract_urls(
        "https://a.example/v\nhttps://a.example/v\nhttps://b.example/v") == [
            "https://a.example/v", "https://b.example/v"]


def test_remote_title_and_template_cannot_escape_output_folder(tmp_path):
    from core.video_downloader import _output_template
    from gui_qt.panels.download_panel import (_safe_download_name,
                                                _validate_template)

    assert "/" not in _safe_download_name("../../outside")
    assert _safe_download_name("CON") == "video"
    with pytest.raises(ValueError):
        _validate_template("../outside.%(ext)s")
    with pytest.raises(ValueError):
        _output_template(str(tmp_path / "video.mp4"), "/tmp/out.%(ext)s")
    assert _output_template(
        str(tmp_path / "video.mp4"), "%(title)s.%(ext)s") == str(
            tmp_path / "%(title)s.%(ext)s")


def test_headers_and_proxy_are_strictly_validated():
    from gui_qt.panels.download_panel import (DownloadPanelPage,
                                                _normalize_proxy)

    assert DownloadPanelPage._parse_headers_from(
        "Referer: https://example.com,User-Agent: Demo") == {
            "Referer": "https://example.com", "User-Agent": "Demo"}
    with pytest.raises(ValueError):
        DownloadPanelPage._parse_headers_from("Broken header")
    with pytest.raises(ValueError):
        DownloadPanelPage._parse_headers_from("X-Test: ok\r\nInjected: yes")
    assert _normalize_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"
    with pytest.raises(ValueError):
        _normalize_proxy("ftp://example.com:21")


def test_cookie_string_is_header_but_cookie_file_uses_cookiefile(tmp_path):
    from core.video_downloader import _apply_network_options

    raw = _apply_network_options({}, "sid=secret", None, {"Referer": "x"})
    assert raw["http_headers"]["Cookie"] == "sid=secret"
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("cookie", encoding="utf-8")
    file_opts = _apply_network_options({}, str(cookie_file), None, None)
    assert file_opts == {"cookiefile": str(cookie_file)}


def test_format_parse_receives_cookie_proxy_and_headers(monkeypatch):
    from core.video_downloader import VideoDownloader

    captured = {}

    def fake_module(self, url, cookie, proxy, headers):
        captured.update(url=url, cookie=cookie, proxy=proxy, headers=headers)
        return [], "", "", None

    monkeypatch.setattr(VideoDownloader, "_get_formats_module", fake_module)
    result = VideoDownloader().get_formats(
        "https://example.com/v", cookie="sid=1",
        proxy="http://127.0.0.1:7890", headers={"Referer": "x"})

    assert result == ([], "", "", None)
    assert captured == {
        "url": "https://example.com/v", "cookie": "sid=1",
        "proxy": "http://127.0.0.1:7890", "headers": {"Referer": "x"}}


def test_download_uses_real_rate_limit_and_reports_ytdlp_error(
        tmp_path, monkeypatch):
    from core import video_downloader
    from utils import config

    captured = {}

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            return 3

    monkeypatch.setitem(
        sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(config, "get_ffmpeg_path", lambda: None)
    messages = []
    ok = video_downloader.VideoDownloader().download(
        "https://example.com/v", str(tmp_path / "video.mp4"),
        speed_limit=5, progress_callback=lambda pct, msg: messages.append((pct, msg)))

    assert not ok
    assert captured["ratelimit"] == 5 * 1024 * 1024
    assert "throttledratelimit" not in captured
    assert "no_check_certificates" not in captured
    assert messages[-1][0] == -1


def test_cli_rejects_raw_cookie_instead_of_leaking_it_to_process_args(
        tmp_path, monkeypatch):
    from core import video_downloader

    monkeypatch.setattr(video_downloader, "_find_ytdlp_exe", lambda: "/tmp/yt-dlp")
    called = []
    monkeypatch.setattr(video_downloader.subprocess, "Popen",
                        lambda *_args, **_kwargs: called.append(True))
    errors = []
    ok = video_downloader.VideoDownloader()._download_cli(
        "https://example.com/v", str(tmp_path / "video.mp4"),
        cookie="sid=secret", progress_callback=lambda pct, msg: errors.append((pct, msg)))

    assert not ok and not called
    assert errors[-1][0] == -1


def test_add_link_does_not_start_parse_or_reuse_stale_title(app_ctx,
                                                            monkeypatch):
    from gui_qt.panels.download_panel import DownloadPanelPage

    app, win, services = app_ctx
    services.task_manager = FakeTaskManager()
    page = DownloadPanelPage(win, services)
    monkeypatch.setattr(page, "_parse_url",
                        lambda: pytest.fail("adding must not start parsing"))
    page._title = "Stale title"
    page._parsed_url = "https://old.example/v"
    page.txt_url.setPlainText("https://new.example/v")
    page._add_url()
    app.processEvents()

    assert page._queue[0]["name"] == "new.example"
    assert page.action_bar.btn_go.isEnabled()
    page.close()
    page.deleteLater()


def test_start_uses_per_task_downloader_unique_paths_and_sensitive_params(
        app_ctx, tmp_path, monkeypatch):
    from core import video_downloader
    from gui_qt.panels.download_panel import DownloadPanelPage

    app, win, services = app_ctx
    manager = FakeTaskManager()
    services.task_manager = manager

    class FakeDownloader:
        instances = []

        def __init__(self):
            self.instances.append(self)

        def download(self, *_args, **_kwargs):
            return True

        def cancel(self):
            pass

    monkeypatch.setattr(video_downloader, "VideoDownloader", FakeDownloader)
    page = DownloadPanelPage(win, services)
    page._queue = [
        {"url": "https://a.example/1", "name": "same", "fmt_id": None,
         "display": "a"},
        {"url": "https://b.example/2", "name": "same", "fmt_id": None,
         "display": "b"},
    ]
    page.lst_queue.addItems(["a", "b"])
    page._update_count()
    page.ed_dir.setText(str(tmp_path))
    page.ed_cookie.setText("sid=secret")
    page.ed_headers.setText("Authorization: secret")
    page._start()
    app.processEvents()

    assert len(manager.tasks) == 2
    assert manager.tasks[0].output_path != manager.tasks[1].output_path
    assert manager.tasks[0].canceller.__self__ is not \
        manager.tasks[1].canceller.__self__
    assert manager.tasks[0].sensitive_param_keys == ("cookie", "headers", "proxy")
    assert manager.tasks[0].allow_auto_recover is False
    assert not page.queue_controls.isEnabled()
    page.close()
    page.deleteLater()


def test_audio_video_modes_and_narrow_layout(app_ctx):
    from gui_qt.panels.download_panel import DownloadPanelPage

    app, win, services = app_ctx
    services.task_manager = FakeTaskManager()
    page = DownloadPanelPage(win, services)
    page.cb_video_only.setChecked(True)
    page.cb_audio_only.setChecked(True)
    assert page.cb_audio_only.isChecked()
    assert not page.cb_video_only.isChecked()

    page.resize(700, 900)
    page.advanced_section.set_expanded(True)
    page.show()
    app.processEvents()
    assert page.url_actions.direction() == QBoxLayout.TopToBottom
    assert page.adv_grid._columns == 1
    assert page.horizontalScrollBar().maximum() == 0
    prefs = page.collect_prefs()
    assert "cookie" not in prefs and "headers" not in prefs and "proxy" not in prefs
    page.close()
    page.deleteLater()
