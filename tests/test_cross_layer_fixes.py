"""整体审查回归：验证输出文件、任务生命周期及前后端契约的真实边界。"""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication
from fastapi.testclient import TestClient

from gui_qt.task_manager import CANCELLED, FAILED, SUCCESS, TaskManager, make_output_path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def manager(qapp, tmp_path, monkeypatch):
    from core.image_converter import ImageConverter

    monkeypatch.setattr(TaskManager, "_snapshot_path", staticmethod(
        lambda: str(tmp_path / "task_queue.json")))
    prefs = SimpleNamespace(get=lambda panel, key, default=None: default,
                            set_now=lambda *args: None, flush=lambda: None)
    services = SimpleNamespace(
        prefs=prefs, image_conv=ImageConverter(),
        get_pref=lambda key, default=None: False if key == "auto_recover" else default,
        ffmpeg_ready=lambda: True, history=SimpleNamespace(add=lambda record: None))
    result = TaskManager(services)
    services.task_manager = result
    result._dispatch_timer.stop()
    yield result
    assert result.shutdown(timeout=3)
    qapp.processEvents()


@pytest.fixture
def image_panel(manager):
    from gui_qt.panels.image_panel import ImagePanelPage

    panel = ImagePanelPage(object(), manager.services)
    yield panel
    panel._prefs_timer.stop()
    panel.close()
    panel.deleteLater()


def _images(tmp_path):
    files = []
    for directory, color in (("a", "red"), ("b", "blue")):
        folder = tmp_path / directory
        folder.mkdir()
        path = folder / "same.png"
        Image.new("RGB", (16, 16), color).save(path)
        files.append(str(path))
    return files


def _add(manager, source, target, runner=lambda task, progress: True):
    tid = manager.add_task("audit", "audit", str(source), str(target), {}, runner,
                           need_ffmpeg=False)
    return manager.get_task(tid)


def test_same_named_images_keep_both_outputs(image_panel, manager, tmp_path):
    files = _images(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    image_panel.out_row.set_state(image_panel.out_row.MODE_CUSTOM, str(output))
    image_panel.cb_fmt.setCurrentText("PNG")
    image_panel.file_card.add_files(files)
    image_panel._start()
    tasks = sorted(manager.all_tasks(), key=lambda task: task.task_id)
    assert len({task.output_path for task in tasks}) == 2
    for task in tasks:
        manager._worker_run(task)
    assert all(task.state == SUCCESS for task in tasks)
    pixels = []
    for task in tasks:
        with Image.open(task.output_path) as image:
            pixels.append(image.getpixel((0, 0)))
    assert pixels == [(255, 0, 0), (0, 0, 255)]


def test_output_reservation_covers_video_and_existing_suffix(manager, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    target = tmp_path / "output.mp4"
    occupied = tmp_path / "output_1.mp4"
    occupied.write_bytes(b"keep")
    first = _add(manager, source, target)
    second_id = manager.add_video_task(str(source), str(target), {})
    second = manager.get_task(second_id)
    assert first.output_path == str(target)
    assert second.output_path == str(tmp_path / "output_2.mp4")
    assert occupied.read_bytes() == b"keep"


def test_cancelled_but_running_output_stays_reserved(manager, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("source")
    target = tmp_path / "out.txt"
    entered, release = threading.Event(), threading.Event()

    def runner(task, progress):
        entered.set()
        assert release.wait(3)
        return False

    first = _add(manager, source, target, runner)
    manager._schedule_next()
    try:
        assert entered.wait(2)
        manager.cancel_task(first.task_id)
        assert manager.clear_completed() == 0
        second = _add(manager, source, target)
        assert second.output_path != first.output_path
    finally:
        release.set()


def test_source_alias_and_existing_suffix_are_protected(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    (tmp_path / "source_1.mp4").write_bytes(b"previous")
    result = make_output_path(str(source), str(tmp_path), ".mp4", conflict="auto_rename")
    assert result == str(tmp_path / "source_1_1.mp4")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    result = make_output_path(str(source), str(alias), ".mp4", conflict="overwrite")
    assert os.path.realpath(result) != str(source)


def test_api_same_source_protects_existing_suffix(tmp_path, monkeypatch):
    import api_server

    source = tmp_path / "input.mp4"
    source.write_bytes(b"input")
    old = tmp_path / "input_1.mp4"
    old.write_bytes(b"keep")

    class Converter:
        def convert(self, source, target, *args):
            Path(target).write_bytes(b"converted")
            return True

    monkeypatch.setattr(api_server, "VideoConverter", Converter)
    response = TestClient(api_server.create_app()).post("/api/video/convert", json={
        "input_path": str(source), "output_path": str(source)})
    assert response.status_code == 200
    assert response.json()["data"]["output_path"] == str(tmp_path / "input_2.mp4")
    assert old.read_bytes() == b"keep"
    assert source.read_bytes() == b"input"


def test_api_concurrent_requests_reserve_different_targets(tmp_path, monkeypatch):
    import api_server

    source = tmp_path / "source.txt"
    source.write_text("source")
    target = tmp_path / "result.pdf"
    barrier = threading.Barrier(2)

    class Converter:
        def convert(self, source, target):
            barrier.wait(timeout=3)
            Path(target).write_bytes(b"converted")
            return True

    monkeypatch.setattr(api_server, "DocumentConverter", Converter)
    app = api_server.create_app()

    def request():
        with TestClient(app) as client:
            return client.post("/api/document/convert", json={
                "input_path": str(source), "output_path": str(target)})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: request(), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert len({r.json()["data"]["output_path"] for r in responses}) == 2


def test_shutdown_wakes_paused_worker_and_preserves_snapshot(manager, tmp_path):
    entered, release = threading.Event(), threading.Event()

    def runner(task, progress):
        entered.set()
        assert release.wait(3)
        progress(20, "working")
        return True

    task = _add(manager, "", tmp_path / "out", runner)
    manager._schedule_next()
    assert entered.wait(2)
    manager.pause_task(task.task_id)
    release.set()
    assert manager.shutdown(timeout=3)
    assert task.state == CANCELLED
    assert not manager._currents
    assert not manager._dispatch_timer.isActive()
    assert manager.add_task("late", "audit", "", "", {}, runner, need_ffmpeg=False) is None
    restored = TaskManager(manager.services)
    try:
        assert len(restored.all_tasks()) == 1
        assert restored.all_tasks()[0].state == FAILED
    finally:
        restored.shutdown()


def test_shutdown_timeout_does_not_release_window_resources(monkeypatch):
    from gui_qt.app import MainWindow
    from gui_qt.components import toast

    cleaned = []
    window = SimpleNamespace(task_manager=SimpleNamespace(shutdown=lambda: False),
                             pages={"test": SimpleNamespace(cleanup=lambda: cleaned.append(True))})
    monkeypatch.setattr(toast, "show_warning", lambda *args: None)
    assert MainWindow._finalize_shutdown(window) is False
    assert not cleaned


def test_paused_worker_does_not_hold_process_open(tmp_path):
    # 独立解释器退出可捕获线程池 atexit 等待，普通线程单测无法验证这一点。
    code = r'''
import sys, threading
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from gui_qt.task_manager import TaskManager
from gui_qt.app import MainWindow
app = QApplication([])
TaskManager._snapshot_path = staticmethod(lambda: sys.argv[1])
services = SimpleNamespace(get_pref=lambda key, default=None: 1 if key == 'parallel' else False,
    ffmpeg_ready=lambda: True, prefs=SimpleNamespace(flush=lambda: None))
manager = TaskManager(services)
entered, release = threading.Event(), threading.Event()
def runner(task, progress):
    entered.set()
    assert release.wait(3)
    progress(10, 'working')
    return True
tid = manager.add_task('paused', 'audit', '', '', {}, runner, need_ffmpeg=False)
manager._schedule_next()
assert entered.wait(2)
manager.pause_task(tid)
release.set()
assert MainWindow._finalize_shutdown(SimpleNamespace(task_manager=manager, services=services, pages={}))
sys.exit(0)
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "queue.json")],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr


def test_image_tasks_have_independent_cancellation(image_panel, manager, tmp_path):
    files = _images(tmp_path)
    kwargs = [image_panel._make_task(path) for path in files]
    assert kwargs[0]["canceller"].__self__ is not kwargs[1]["canceller"].__self__
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()

    def run(index):
        def progress(pct, message):
            if pct == 20:
                ready[index].set()
                assert release.wait(3)
        return kwargs[index]["runner"](SimpleNamespace(**kwargs[index]), progress)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, index) for index in range(2)]
        try:
            assert all(event.wait(2) for event in ready)
            kwargs[0]["canceller"]()
        finally:
            release.set()
        assert [future.result() for future in futures] == [False, True]


def test_retry_waits_for_old_worker_then_runs_again(manager, tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("source")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def runner(task, progress):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        progress(50, "work")
        return True

    task = _add(manager, source, tmp_path / "out.txt", runner)
    manager._schedule_next()
    try:
        assert entered.wait(2)
        manager.cancel_task(task.task_id)
        assert not manager.retry_task(task.task_id)
        workers = list(manager._workers)
    finally:
        release.set()
    for future in workers:
        future.result(timeout=3)
    assert task.state == CANCELLED
    assert manager.retry_task(task.task_id)
    manager._worker_run(task)
    assert task.state == SUCCESS
    assert len(calls) == 2


@pytest.mark.parametrize("terminal", [SUCCESS, CANCELLED, FAILED])
def test_terminal_queue_removes_stale_snapshot(manager, tmp_path, terminal):
    task = _add(manager, "", tmp_path / "out")
    manager.flush_snapshot()
    assert Path(manager._snapshot_path()).exists()
    manager._set_state(task, terminal)
    manager.flush_snapshot()
    assert not Path(manager._snapshot_path()).exists()
    restored = TaskManager(manager.services)
    try:
        assert restored.all_tasks() == []
    finally:
        restored.shutdown()


@pytest.mark.parametrize("password", ["中文访问密码", "secret123"])
def test_lan_password_unicode_and_bad_unicode_token(password):
    from core.lan_service import LanService

    service = LanService()
    service.set_access_token(password)
    client = TestClient(service._app, follow_redirects=False)
    assert client.get("/chat").status_code == 200
    assert client.get("/chat/history", params={"token": "错误密码"}).status_code == 401
    response = client.post("/chat/login", data={"token": password})
    assert response.status_code == 302
    assert client.get("/chat/history").status_code == 200


@pytest.fixture
def media(tmp_path):
    from utils.config import get_ffmpeg_path, get_ffprobe_path

    ffmpeg, ffprobe = get_ffmpeg_path(), get_ffprobe_path()
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/FFprobe unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True, timeout=20)
    return source, ffmpeg, ffprobe


@pytest.mark.parametrize("extension,expected_codec", [(".webm", "vp9"), (".gif", "gif")])
def test_default_container_conversion_with_audio(media, tmp_path, extension, expected_codec):
    from core.video_converter import VideoConverter

    source, ffmpeg, ffprobe = media
    output = tmp_path / ("converted" + extension)
    messages = []
    assert VideoConverter().convert(str(source), str(output), extension,
                                    progress_callback=lambda pct, msg: messages.append(msg)), messages
    info = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output)],
                          capture_output=True, text=True, check=True, timeout=10)
    streams = json.loads(info.stdout)["streams"]
    assert next(s for s in streams if s["codec_type"] == "video")["codec_name"] == expected_codec
    if extension == ".webm":
        assert next(s for s in streams if s["codec_type"] == "audio")["codec_name"] == "opus"
    else:
        assert not any(s["codec_type"] == "audio" for s in streams)


def test_api_rejects_incompatible_codec_without_writing(tmp_path):
    import api_server

    source = tmp_path / "source.mp4"
    source.write_bytes(b"unused")
    target = tmp_path / "output.webm"
    response = TestClient(api_server.create_app()).post("/api/video/convert", json={
        "input_path": str(source), "output_path": str(target), "format": "webm", "codec": "h264"})
    assert response.status_code == 422
    assert not target.exists()


def test_video_panel_filters_incompatible_codecs(manager, monkeypatch):
    from gui_qt.panels.video_panel import VideoPanelPage

    monkeypatch.setattr(VideoPanelPage, "_ensure_hw_options_async", lambda self: None)
    panel = VideoPanelPage(object(), manager.services)
    try:
        panel.cb_fmt.setCurrentText("WEBM")
        assert "H.264" not in [panel.cb_codec.itemText(i) for i in range(panel.cb_codec.count())]
        assert not panel.cb_hw.isEnabled()
        panel.cb_fmt.setCurrentText("GIF")
        assert panel.cb_codec.count() == 1
        assert not panel.cb_copy.isEnabled()
        panel.cb_fmt.setCurrentText("MP4")
        assert "H.264" in [panel.cb_codec.itemText(i) for i in range(panel.cb_codec.count())]
        assert panel.cb_copy.isEnabled()
    finally:
        panel._prefs_timer.stop()
        panel.close()
        panel.deleteLater()


def test_actual_ffmpeg_progress_fields_agree(media):
    from core.ffmpeg_progress import _parse_time_line

    source, ffmpeg, ffprobe = media
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(source), "-c", "copy",
                          "-progress", "pipe:1", "-f", "null", "-"],
                         capture_output=True, text=True, check=True, timeout=10).stdout
    values = {key: [] for key in ("out_time_us", "out_time_ms")}
    for line in raw.splitlines():
        key = line.split("=", 1)[0]
        if key in values:
            values[key].append(_parse_time_line(line))
    assert values["out_time_us"] == values["out_time_ms"]
    assert values["out_time_us"] and max(values["out_time_us"]) < 2
    assert _parse_time_line("out_time=00:00:00.800000") == pytest.approx(0.8)


def test_remove_row_does_not_relabel_another_task(image_panel, manager, tmp_path):
    image_panel.file_card.add_files(_images(tmp_path))
    image_panel._start()
    first, second = sorted(manager.all_tasks(), key=lambda task: task.task_id)
    image_panel.file_card.remove_row(0)
    manager._set_state(first, SUCCESS)
    assert image_panel.file_card.files() == [second.file_path]
    assert image_panel.file_card.table.item(0, 3).text() != "已完成"
    manager._set_state(second, SUCCESS)
    assert image_panel.file_card.table.item(0, 3).text() == "已完成"


@pytest.mark.parametrize("bad_value", [["x", "y"], {"bad": "value"}, 42, None])
def test_invalid_recv_directory_uses_default(bad_value):
    from gui_qt.panels.lan_transfer_panel import LanTransferPanelPage

    fake = SimpleNamespace(services=SimpleNamespace(get_pref=lambda *args: bad_value),
                           _default_recv_dir=lambda: "fallback")
    assert LanTransferPanelPage._preferred_recv_dir(fake) == "fallback"
