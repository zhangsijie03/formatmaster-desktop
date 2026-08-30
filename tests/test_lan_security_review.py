"""局域网服务的路径、上传边界、鉴权、资源上限与页面回归。"""

import asyncio
import io
import os
import time
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from fastapi.testclient import TestClient
from PySide6.QtWidgets import QApplication, QBoxLayout


def _service(tmp_path):
    from core.lan_service import LanService

    service = LanService(port=8787)
    received = tmp_path / "received"
    chat = tmp_path / "chat"
    service.set_recv(str(received))
    chat.mkdir()
    service.chat_dir = str(chat)
    return service, received, chat


def test_multipart_path_traversal_and_symlink_escape_are_rejected(tmp_path):
    service, received, _chat = _service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), str(received / "link"))
    client = TestClient(service._app)

    traversal = client.post(
        "/chat/file?side=phone",
        files={"file": ("../escape.txt", b"escape")})
    symlink = client.post(
        "/chat/file?side=phone",
        files={"file": ("link/escape.txt", b"escape")})

    assert traversal.status_code == 200 and traversal.json()["count"] == 0
    assert symlink.status_code == 200 and symlink.json()["count"] == 0
    assert not (tmp_path / "escape.txt").exists()
    assert not (outside / "escape.txt").exists()


def test_truncated_multipart_preserves_existing_overwrite_target(tmp_path):
    from core.lan_receiver import _parse_multipart_stream

    target = tmp_path / "existing.txt"
    target.write_bytes(b"keep-existing")
    boundary = b"review-boundary"
    truncated = (
        b"--review-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="existing.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\nreplacement-without-final-boundary")
    from io import BytesIO

    _parse_multipart_stream(
        BytesIO(truncated), boundary, str(tmp_path), "overwrite", time.time())

    assert target.read_bytes() == b"keep-existing"
    assert not list(tmp_path.glob(".fm_recv_*"))


def test_complete_multipart_atomically_replaces_target(tmp_path):
    from core.lan_receiver import _parse_multipart_stream
    from io import BytesIO

    target = tmp_path / "existing.txt"
    target.write_bytes(b"old")
    body = (
        b"--done\r\n"
        b'Content-Disposition: form-data; name="file"; filename="existing.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\nnew-content\r\n--done--\r\n")
    _parse_multipart_stream(
        BytesIO(body), b"done", str(tmp_path), "overwrite", time.time())
    assert target.read_bytes() == b"new-content"
    assert not list(tmp_path.glob(".fm_recv_*"))


def test_request_spooling_rejects_actual_and_declared_oversize():
    from core.lan_service import _spool_request

    class FakeRequest:
        def __init__(self, chunks, declared="0"):
            self.headers = {"content-length": declared}
            self._chunks = chunks

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    with pytest.raises(ValueError):
        asyncio.run(_spool_request(FakeRequest([b"1234"], "99"), 8))
    with pytest.raises(ValueError):
        asyncio.run(_spool_request(FakeRequest([b"12345", b"6789"]), 8))


def test_chunk_upload_rejects_bad_name_index_and_oversize(tmp_path,
                                                           monkeypatch):
    from core import lan_service

    service, _received, _chat = _service(tmp_path)
    client = TestClient(service._app)
    bad = client.post("/chat/upload/init", json={
        "name": "../outside.bin", "size": 3, "total_chunks": 1})
    assert bad.status_code == 400

    good = client.post("/chat/upload/init", json={
        "name": "inside.bin", "size": 3, "total_chunks": 1,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"})
    sid = good.json()["sid"]
    assert client.post(
        f"/chat/upload/chunk?sid={sid}&index=1", content=b"abc").status_code == 400
    monkeypatch.setattr(lan_service, "MAX_CHUNK_BYTES", 2)
    assert client.post(
        f"/chat/upload/chunk?sid={sid}&index=0", content=b"abc").status_code == 413


def test_password_cookie_hides_secret_rate_limits_and_rotates(tmp_path):
    service, _received, _chat = _service(tmp_path)
    service.set_access_token("first-secret")
    client = TestClient(service._app, follow_redirects=False)
    login = client.post("/chat/login", data={"token": "first-secret"})
    cookie = login.headers["set-cookie"]
    assert "fm_session=" in cookie and "first-secret" not in cookie
    assert client.get("/chat/history").status_code == 200

    service.set_access_token("second-secret")
    assert client.get("/chat/history").status_code == 401
    attacker = TestClient(service._app, follow_redirects=False)
    statuses = [attacker.post("/chat/login", data={"token": "wrong"}).status_code
                for _ in range(11)]
    assert statuses[-1] == 429


def test_chat_session_has_bounded_messages_and_devices(monkeypatch):
    from core import lan_service

    monkeypatch.setattr(lan_service, "MAX_CHAT_MESSAGES", 3)
    session = lan_service.ChatSession()
    for index in range(5):
        session.add("phone", "text", text=str(index))
    assert [message["text"] for message in session.since(0)] == ["2", "3", "4"]

    for index in range(205):
        session.heartbeat(f"device-{index}")
    assert len(session._devices) <= 200


def test_stop_cleans_unfinished_upload_directories(tmp_path):
    service, _received, chat = _service(tmp_path)
    upload_dir = chat / ".uploads" / "session"
    upload_dir.mkdir(parents=True)
    service._uploads["session"] = {
        "dir": str(upload_dir), "meta": {}, "chunks": set(),
        "chunk_sizes": {}, "lock": __import__("threading").Lock()}
    assert service.stop() is True
    assert not upload_dir.exists() and service._uploads == {}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_panel_defaults_to_protected_session_and_reflows(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.lan_transfer_panel import LanTransferPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager
    from utils.config import USER_PREFS

    USER_PREFS.set("qt_app", "lan_token", "legacy-plaintext")
    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    panel = LanTransferPanelPage(object(), services)
    try:
        assert len(panel.ed_token.text()) == 8
        assert USER_PREFS.get("qt_app", "lan_token", "x") == ""
        assert panel.cb_ip.isHidden() is False
        assert panel.btn_eye.accessibleName()
        panel.resize(700, 800)
        panel.show()
        app.processEvents()
        assert panel._cfg_lay.direction() == QBoxLayout.TopToBottom
        assert panel.horizontalScrollBar().maximum() == 0
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_chat_page_accessibility_and_reduced_motion_markers():
    from core.lan_chat_page import _CHAT_HTML

    assert "prefers-reduced-motion:reduce" in _CHAT_HTML
    assert 'aria-live="polite"' in _CHAT_HTML
    assert 'aria-label="关闭预览"' in _CHAT_HTML
    assert "button:focus-visible" in _CHAT_HTML
    assert '<button class="ctx-item"' in _CHAT_HTML


def test_share_page_routes_accessibility_and_selected_query(tmp_path):
    from core.lan_service import LanService

    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    service = LanService(port=8788)
    service.share_dir = str(tmp_path)
    downloaded = []
    service.on_downloaded = lambda name, size, sec, ip: downloaded.append(name)
    client = TestClient(service._app)

    page = client.get("/share/")
    assert page.status_code == 200
    assert "fetchDL('/all.zip'" in page.text
    assert "return 'files='+encodeURIComponent(n)" in page.text
    assert 'aria-label="搜索共享文件"' in page.text
    assert 'aria-label="关闭预览"' in page.text
    assert "prefers-reduced-motion:reduce" in page.text

    selected = client.get(
        "/selected.zip", params=[("files", "a.txt"), ("files", "b.txt")])
    assert selected.status_code == 200
    with zipfile.ZipFile(io.BytesIO(selected.content)) as archive:
        assert sorted(archive.namelist()) == ["a.txt", "b.txt"]

    all_files = client.get("/all.zip")
    assert all_files.status_code == 200
    with zipfile.ZipFile(io.BytesIO(all_files.content)) as archive:
        assert sorted(archive.namelist()) == ["a.txt", "b.txt"]
    assert downloaded == ["selected.zip", "all.zip"]


def test_start_share_renames_duplicate_basenames(tmp_path):
    from core.lan_service import LanService

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same.txt").write_text("first")
    (second / "same.txt").write_text("second")
    service = LanService(port=8789)
    try:
        shared = service.start_share(
            [str(first / "same.txt"), str(second / "same.txt")])
        names = sorted(os.listdir(shared))
        assert names == ["same.txt", "same_1.txt"]
        assert {Path(shared, name).read_text() for name in names} == {
            "first", "second"}
    finally:
        service.clear_share()


def test_make_zip_has_single_root_and_skips_symlinks(tmp_path):
    from core.lan_sender import make_zip

    source = tmp_path / "photos"
    source.mkdir()
    (source / "photo.txt").write_text("inside")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (source / "secret-link.txt").symlink_to(secret)
    output = tmp_path / "output"
    output.mkdir()

    archive_path = make_zip([str(source)], str(output), "photos")
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["photos/photo.txt"]
        assert archive.read("photos/photo.txt") == b"inside"


def test_share_archive_limits_return_413(tmp_path, monkeypatch):
    from core import lan_transfer
    from core.lan_service import LanService

    (tmp_path / "large.bin").write_bytes(b"12")
    service = LanService(port=8790)
    service.share_dir = str(tmp_path)
    monkeypatch.setattr(lan_transfer, "MAX_SHARE_ARCHIVE_BYTES", 1)
    response = TestClient(service._app).get("/all.zip")
    assert response.status_code == 413
