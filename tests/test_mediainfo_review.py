"""媒体信息核心格式化、异步竞争和结果导出回归测试。"""

import os
from types import SimpleNamespace

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
    from gui_qt.panels.mediainfo_panel import MediaInfoPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = MediaInfoPanelPage(SimpleNamespace(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30000/1001", "29.97 fps"), ("0/0", "--"),
     ("N/A", "--"), ("nan", "--"), ("25", "25 fps")],
)
def test_frame_rate_formatting_is_defensive(value, expected):
    from core.mediainfo import _fmt_ratio

    assert _fmt_ratio(value) == expected


def test_non_finite_duration_is_rejected():
    from core.mediainfo import _fmt_dur

    assert _fmt_dur("nan") == "--"
    assert _fmt_dur("inf") == "--"


def test_core_uses_real_size_safe_dimensions_and_per_type_stream_numbers(
        monkeypatch, tmp_path):
    from core import mediainfo

    source = tmp_path / "sample.mp4"
    source.write_bytes(b"1234567")
    monkeypatch.setattr(
        mediainfo, "get_ffprobe_raw",
        lambda _path: {
            "format": {"format_name": "mov,mp4", "duration": "2"},
            "streams": [
                {"codec_type": "audio", "codec_name": "aac", "channels": 2},
                {"codec_type": "video", "codec_name": "h264",
                 "avg_frame_rate": "0/0"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 1},
            ],
        })

    sections = mediainfo.get_mediainfo(str(source))

    assert sections[0][1][1] == ("文件大小", "7 B")
    assert [title for title, _pairs in sections[1:]] == [
        "音频流 1", "视频流 1", "音频流 2"]
    video = dict(sections[2][1])
    assert video["分辨率"] == "--"
    assert video["帧率"] == "--"
    assert dict(sections[1][1])["声道"] == "2"


def test_single_click_selects_the_file_to_inspect(panel, monkeypatch, tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    inspected = []
    monkeypatch.setattr(panel, "_inspect", inspected.append)

    panel.file_card.add_files([str(first), str(second)])
    panel.file_card.table.selectRow(1)

    assert inspected[0] == str(first)
    assert inspected[-1] == str(second)


def test_stale_worker_result_cannot_overwrite_current_selection(
        panel, monkeypatch, tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(panel, "_inspect", lambda _path: None)
    panel.file_card.add_files([str(first), str(second)])
    panel.file_card.table.selectRow(1)
    panel._request_serial = 2
    panel._current_path = str(second)
    old_sections = [("文件", [("标题", "old")])]
    new_sections = [("文件", [("标题", "new")])]

    panel._on_done(1, str(first), old_sections)
    assert panel.table.rowCount() == 0
    panel._on_done(2, str(second), new_sections)

    assert panel._sections == new_sections
    assert panel.table.item(1, 1).text() == "new"
    assert panel.file_card.table.item(1, 3).text() in ("已读取", "Loaded")


def test_worker_exceptions_reach_safe_worker_error_signal(monkeypatch):
    from gui_qt.panels.mediainfo_panel import _InfoWorker

    monkeypatch.setattr(
        "core.mediainfo.get_mediainfo",
        lambda _path: (_ for _ in ()).throw(ValueError("broken metadata")))
    errors = []
    worker = _InfoWorker("sample.mp4")
    worker.sig_error.connect(errors.append)

    worker.run()

    assert errors == ["broken metadata"]


def test_result_text_can_be_exported_atomically(panel, monkeypatch, tmp_path):
    output = tmp_path / "info.txt"
    panel._current_path = "sample.mp4"
    panel._sections = [("文件", [("容器格式", "MPEG-4"), ("时长", "00:00:02.00")])]
    panel._sync_result_actions()
    monkeypatch.setattr(
        "gui_qt.panels.mediainfo_panel.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(output), "Text"))
    monkeypatch.setattr(
        "gui_qt.panels.mediainfo_panel.toast.show_success",
        lambda *_args, **_kwargs: None)

    panel._export_text()

    text = output.read_text(encoding="utf-8")
    assert "MPEG-4" in text
    assert "00:00:02.00" in text


def test_missing_ffprobe_restores_reload_button(panel, monkeypatch, tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(panel, "_inspect", lambda _path: None)
    panel.file_card.add_files([str(source)])
    monkeypatch.undo()
    monkeypatch.setattr("utils.config.get_ffprobe_path", lambda: None)
    monkeypatch.setattr(
        "gui_qt.panels.mediainfo_panel.toast.show_error",
        lambda *_args, **_kwargs: None)

    panel._inspect(str(source))

    assert panel.btn_inspect.isEnabled()
    assert panel.file_card.table.item(0, 3).text() in ("读取失败", "Failed")


def test_page_uses_header_actions_and_narrow_layout(panel, app):
    from gui_qt.components.page_header import PageHeader

    panel.resize(720, 760)
    panel.show()
    app.processEvents()
    assert isinstance(panel.header, PageHeader)
    assert panel.action_bar.parent() is panel.header
    assert panel.horizontalScrollBar().maximum() == 0
