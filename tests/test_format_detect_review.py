"""格式检测菜单的内容识别、状态恢复与路由回归测试。"""

import os
import zipfile
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.detect_panel import DetectPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    window = SimpleNamespace(pages={})
    page = DetectPanelPage(window, services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_content_detection_handles_iso_media_zip_office_and_svg(tmp_path):
    from gui_qt.panels.detect_panel import detect_format_by_content

    mp4 = tmp_path / "movie.bin"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    heic = tmp_path / "photo.bin"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20)
    svg = tmp_path / "vector.data"
    svg.write_text('<?xml version="1.0"?><svg width="10"/>', encoding="utf-8")
    office = tmp_path / "renamed.zip"
    with zipfile.ZipFile(office, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")

    assert detect_format_by_content(str(mp4)) == "video"
    assert detect_format_by_content(str(heic)) == "image"
    assert detect_format_by_content(str(svg)) == "image"
    assert detect_format_by_content(str(office)) == "doc"


def test_scan_worker_prefers_content_and_orders_files(tmp_path):
    from gui_qt.panels.detect_panel import _ScanWorker

    disguised = tmp_path / "b.jpg"
    disguised.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 20)
    results = []
    worker = _ScanWorker(str(tmp_path), [False])
    worker.sig_done.connect(lambda detected, info: results.append((detected, info)))

    worker.work()

    detected, info = results[0]
    assert detected["video"] == [str(disguised)]
    assert detected["audio"] == [str(audio)]
    assert [os.path.basename(item[0]) for item in info] == ["a.mp3", "b.jpg"]
    disguised_info = next(item for item in info if item[0] == str(disguised))
    assert disguised_info[1:] == ("video", "32 B", "image", "video")


def test_empty_folder_restores_idle_instead_of_success(panel):
    detected = {key: [] for key in (
        "video", "audio", "image", "doc", "pdf", "other")}
    panel._phase = "scanning"
    panel._on_scan_done(detected, [])

    assert panel._phase == "idle"
    assert panel.btn_go.isEnabled()
    assert "为空" in panel.lb_status.text() or "empty" in panel.lb_status.text().lower()


def test_worker_error_restores_controls_and_reports_failure(panel, monkeypatch):
    errors = []
    monkeypatch.setattr(
        "gui_qt.panels.detect_panel.toast.show_error",
        lambda _parent, message: errors.append(message))
    panel._phase = "scanning"
    panel._set_scan_controls(False)
    panel.action_bar.set_running(True)

    panel._on_scan_error("permission denied")

    assert panel._phase == "idle"
    assert panel.ed_path.isEnabled()
    assert panel.btn_browse.isEnabled()
    assert panel.cb_auto_add.isEnabled()
    assert errors and "permission denied" in errors[-1]


def test_extension_mismatch_is_visible_but_not_routed(panel, tmp_path):
    disguised = tmp_path / "movie.jpg"
    disguised.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    detected = {key: [] for key in (
        "video", "audio", "image", "doc", "pdf", "other")}
    detected["video"] = [str(disguised)]
    info = [(str(disguised), "video", "32 B", "image", "video")]

    panel._show_results(detected, info)

    _path, category, check_item = panel._rows[0]
    assert category == "video"
    assert check_item.checkState() == Qt.Unchecked
    assert not check_item.flags() & Qt.ItemIsUserCheckable
    assert not panel.btn_go.isEnabled()
    assert "⚠️" in panel.table.item(1, 1).text()


def test_submit_starts_page_even_when_auto_add_already_added_files(panel):
    starts = []

    class Card:
        def add_files(self, _files):
            return 0

    target = SimpleNamespace(file_card=Card(), _start=lambda: starts.append(True))
    panel.main_window.pages = {"video": target}

    added, unavailable = panel._add_to_panels(
        {"video": ["already-added.mp4"]}, submit=True)

    assert added == 0
    assert unavailable == 0
    assert starts == [True]


def test_page_uses_header_actions_and_survives_narrow_layout(panel, app):
    from gui_qt.components.page_header import PageHeader

    panel.resize(720, 760)
    panel.show()
    app.processEvents()
    assert isinstance(panel.header, PageHeader)
    assert panel.action_bar.parent() is panel.header
    assert panel.horizontalScrollBar().maximum() == 0

