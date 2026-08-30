"""M3U8 下载菜单审查回归：输入边界、任务隔离、续传与原子输出。"""

import subprocess
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


def test_urls_names_headers_and_proxy_are_strictly_validated():
    from gui_qt.panels.m3u8_panel import (
        _clean_m3u8_url, _extract_m3u8_urls, _normalize_proxy,
        _parse_headers, _safe_download_name, _safe_subtitle_lang,
    )

    assert _clean_m3u8_url("plain text") == ""
    assert _clean_m3u8_url("https://example.com:99999/a.m3u8") == ""
    assert _clean_m3u8_url("分享 https://a.example/live.m3u8。") == \
        "https://a.example/live.m3u8"
    assert _extract_m3u8_urls(
        "https://a.example/1\nhttps://a.example/1\nhttps://b.example/2") == [
            "https://a.example/1", "https://b.example/2"]
    assert "/" not in _safe_download_name("../../outside")
    assert _safe_download_name("CON") == "video"
    assert _safe_subtitle_lang("../../outside") == "______outside"
    assert _normalize_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"
    with pytest.raises(ValueError):
        _normalize_proxy("ftp://example.com")
    with pytest.raises(ValueError):
        _normalize_proxy("http://user:secret@example.com:8080")
    with pytest.raises(ValueError):
        _parse_headers("Broken header")
    with pytest.raises(ValueError):
        _parse_headers("X-Test: ok\r\nInjected: yes")


def test_quality_is_only_applied_to_its_single_source(app_ctx, monkeypatch):
    from gui_qt.panels.m3u8_panel import M3u8PanelPage

    app, win, services = app_ctx
    services.task_manager = FakeTaskManager()
    page = M3u8PanelPage(win, services)
    monkeypatch.setattr(page, "_parse_url_for", lambda _url: None)
    page._quality_source_url = "https://a.example/master.m3u8"
    page._qualities = [{"url": "https://cdn.example/high.m3u8",
                        "label": "1080p"}]
    page.cb_quality.clear()
    page.cb_quality.addItem("1080p")
    page.txt_url.setPlainText(
        "https://a.example/master.m3u8\nhttps://b.example/master.m3u8")
    # 输入变化会主动废弃旧解析；模拟解析完成后再批量添加。
    page._quality_source_url = "https://a.example/master.m3u8"
    page._qualities = [{"url": "https://cdn.example/high.m3u8",
                        "label": "1080p"}]
    page._batch_add()
    app.processEvents()

    assert [item["url"] for item in page._queue] == [
        "https://a.example/master.m3u8", "https://b.example/master.m3u8"]
    page.close()
    page.deleteLater()


def test_start_isolates_downloaders_paths_and_sensitive_params(
        app_ctx, tmp_path, monkeypatch):
    from core import m3u8_downloader
    from gui_qt.panels.m3u8_panel import M3u8PanelPage

    app, win, services = app_ctx
    manager = FakeTaskManager()
    services.task_manager = manager

    class FakeDownloader:
        def __init__(self):
            self.store = SimpleNamespace()

        def download(self, *_args, **_kwargs):
            return True

        def cancel(self):
            pass

    monkeypatch.setattr(m3u8_downloader, "M3U8Downloader", FakeDownloader)
    page = M3u8PanelPage(win, services)
    page._queue = [
        {"url": "https://a.example/1", "master_url": "https://a.example/1",
         "name": "same", "display": "a"},
        {"url": "https://b.example/2", "master_url": "https://b.example/2",
         "name": "same", "display": "b"},
    ]
    page.lst_queue.addItems(["a", "b"])
    page.ed_dir.setText(str(tmp_path))
    page.ed_cookie.setText("sid=secret")
    page.ed_headers.setText("Authorization: secret")
    monkeypatch.setattr(services, "ffmpeg_ready", lambda: True)
    page._start()
    app.processEvents()

    assert len(manager.tasks) == 2
    assert manager.tasks[0].output_path != manager.tasks[1].output_path
    assert manager.tasks[0].canceller.__self__ is not \
        manager.tasks[1].canceller.__self__
    assert manager.tasks[0].sensitive_param_keys == ("cookie", "headers", "proxy")
    assert manager.tasks[0].allow_auto_recover is False
    assert not page.queue_controls.isEnabled()
    assert "cookie" not in page.collect_prefs()
    assert "headers" not in page.collect_prefs()
    assert "proxy" not in page.collect_prefs()
    page.close()
    page.deleteLater()


def test_resume_reuses_stable_segments_and_cross_host_urls(tmp_path,
                                                            monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    output = tmp_path / "video.ts"
    segment_dir = tmp_path / "video.ts.segments"
    segment_dir.mkdir()
    (segment_dir / "seg_000000.ts").write_bytes(b"first")
    (tmp_path / "video.ts.progress").write_text(
        '{"downloaded": [0], "total_bytes": 999}', encoding="utf-8")
    downloader = M3U8Downloader()
    monkeypatch.setattr(
        downloader, "_parse_m3u8",
        lambda *_args, **_kwargs: [
            "https://one.example/a.ts", "https://cdn.example/b.ts"])
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return b"second"

    class Opener:
        def open(self, request, timeout=0):
            requested.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    assert downloader._download_with_threads(
        "https://master.example/live.m3u8", str(output), threads=2)
    assert requested == [("https://cdn.example/b.ts", 30)]
    assert output.read_bytes() == b"firstsecond"
    assert not segment_dir.exists()
    assert not (tmp_path / "video.ts.progress").exists()


def test_master_playlist_uses_highest_variant_and_urljoin(monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    downloader = M3U8Downloader()
    monkeypatch.setattr(
        downloader, "get_qualities", lambda *_args, **_kwargs: [{
            "url": "https://cdn.example/high/playlist.m3u8"}])
    payloads = {
        "https://origin.example/master.m3u8":
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n",
        "https://cdn.example/high/playlist.m3u8":
            b"#EXTM3U\n#EXTINF:5,\n../segment.ts?token=1\n",
    }

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.data

    class Opener:
        def open(self, request, timeout=0):
            return Response(payloads[request.full_url])

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    assert downloader._parse_m3u8(
        "https://origin.example/master.m3u8") == [
            "https://cdn.example/segment.ts?token=1"]


def test_playlist_rejects_local_file_segments(monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"#EXTM3U\n#EXTINF:5,\nfile:///etc/passwd\n"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    assert M3U8Downloader()._parse_m3u8(
        "https://example.com/live.m3u8") == []


def test_missing_segment_preserves_existing_output_and_resume_data(
        tmp_path, monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    output = tmp_path / "video.ts"
    output.write_bytes(b"old-good-output")
    downloader = M3U8Downloader()
    monkeypatch.setattr(
        downloader, "_parse_m3u8",
        lambda *_args, **_kwargs: ["https://cdn.example/missing.ts"])

    class Opener:
        def open(self, *_args, **_kwargs):
            raise OSError("offline")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    assert not downloader._download_with_threads(
        "https://master.example/live.m3u8", str(output), threads=1)
    assert output.read_bytes() == b"old-good-output"
    assert (tmp_path / "video.ts.progress").exists()


def test_incomplete_subtitle_playlist_preserves_existing_file(
        tmp_path, monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    output = tmp_path / "video.zh.vtt"
    output.write_text("old subtitle", encoding="utf-8")

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.data

    class Opener:
        def open(self, request, timeout=0):
            if request.full_url.endswith("playlist.m3u8"):
                return Response(b"#EXTM3U\n#EXTINF:5,\npart.vtt\n")
            raise OSError("segment unavailable")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    assert not M3U8Downloader().download_subtitle(
        "https://example.com/playlist.m3u8", str(output))
    assert output.read_text(encoding="utf-8") == "old subtitle"
    assert not (tmp_path / "video.zh.vtt.part").exists()


def test_cleanup_does_not_delete_unrelated_ts_files(tmp_path):
    from core.m3u8_downloader import M3U8Downloader

    unrelated = tmp_path / "another-video.ts"
    unrelated.write_bytes(b"keep")
    output = tmp_path / "video.mp4"
    owned = tmp_path / "video.mp4.segments"
    owned.mkdir()
    (owned / "seg_000000.ts").write_bytes(b"remove")

    M3U8Downloader()._cleanup_ts_files(str(output))

    assert unrelated.read_bytes() == b"keep"
    assert not owned.exists()


def test_non_ts_segment_merge_is_remuxed_before_success(tmp_path, monkeypatch):
    from core import m3u8_downloader
    from utils import config

    merge = tmp_path / "video.mp4.merge"
    merge.write_bytes(b"transport-stream")
    output = tmp_path / "video.mp4"
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **_kwargs):
            commands.append(command)
            with open(command[-1], "wb") as file:
                file.write(b"real-mp4")

        def poll(self):
            return 0

    monkeypatch.setattr(config, "get_ffmpeg_path", lambda: "/tmp/ffmpeg")
    monkeypatch.setattr(subprocess, "Popen", Process)
    downloader = m3u8_downloader.M3U8Downloader()

    assert downloader._finalize_segment_merge(str(merge), str(output))
    assert commands[0][-1].endswith(".part.mp4")
    assert output.read_bytes() == b"real-mp4"
    assert not merge.exists()


def test_ytdlp_receives_custom_headers_and_requires_real_output(
        tmp_path, monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    captured = {}
    output = tmp_path / "video.mp4"

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            output.write_bytes(b"video")

    monkeypatch.setitem(
        sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    assert M3U8Downloader()._try_ytdlp(
        "https://example.com/live.m3u8", str(output), cookie="sid=1",
        headers={"Referer": "https://example.com"})
    assert captured["http_headers"] == {
        "Referer": "https://example.com", "Cookie": "sid=1"}


def test_ffmpeg_fallback_does_not_expose_cookie_in_process_args(
        tmp_path, monkeypatch):
    from core.m3u8_downloader import M3U8Downloader

    called = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *_args, **_kwargs: called.append(True))
    messages = []
    assert not M3U8Downloader()._try_ffmpeg(
        "https://example.com/live.m3u8", str(tmp_path / "video.mp4"),
        progress_callback=lambda pct, msg: messages.append((pct, msg)),
        cookie="sid=secret")
    assert not called
    assert messages[-1][0] == -1


def test_narrow_layout_has_no_horizontal_scroll(app_ctx):
    from gui_qt.panels.m3u8_panel import M3u8PanelPage

    app, win, services = app_ctx
    services.task_manager = FakeTaskManager()
    page = M3u8PanelPage(win, services)
    page.resize(700, 900)
    page.show()
    app.processEvents()

    assert page.url_actions.direction() == QBoxLayout.TopToBottom
    assert page.settings_grid._columns == 1
    assert page.horizontalScrollBar().maximum() == 0
    page.close()
    page.deleteLater()
