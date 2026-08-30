"""媒体信息页的信息层级、状态反馈与响应式回归测试。"""

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


def _sections():
    return [
        ("文件", [("容器格式", "MPEG-4"), ("时长", "00:00:12.00")]),
        ("视频流 1", [("编码器", "H.264")]),
        ("音频流 1", [("编码器", "AAC")]),
        ("音频流 2", [("编码器", "AAC")]),
        ("字幕流 1", [("语言", "eng")]),
    ]


def test_multi_file_selection_and_source_safety_are_explicit(panel):
    text = panel.file_hint.text().lower()
    assert "多个" in text or "multiple" in text
    assert "不会修改" in text or "never changes" in text


def test_empty_state_explains_expected_details(panel):
    text = panel.result_hint.text().lower()
    assert "容器" in text or "container" in text
    assert "编码" in text or "codec" in text


def test_loaded_summary_identifies_file_and_stream_counts(panel):
    panel._sync_result_hint("/tmp/interview-final.mp4", _sections())

    text = panel.result_hint.text()
    assert "interview-final.mp4" in text
    assert "1" in text
    assert "2" in text
    assert "Data: 0" in text or "数据流 0" in text


def test_loading_and_error_states_name_current_file(panel, monkeypatch):
    source = "/tmp/interview-final.mp4"
    monkeypatch.setattr(os.path, "isfile", lambda path: path == source)
    monkeypatch.setattr("utils.config.get_ffprobe_path", lambda: "/tmp/ffprobe")

    class Signal:
        def connect(self, _callback):
            pass

    class Worker:
        sig_done = Signal()
        sig_error = Signal()
        finished = Signal()

        def __init__(self, *_args):
            pass

        def stop(self):
            pass

        def wait(self, _timeout):
            return True

        def start(self):
            pass

    monkeypatch.setattr("gui_qt.panels.mediainfo_panel._InfoWorker", Worker)
    panel._inspect(source)
    assert "interview-final.mp4" in panel.result_hint.text()

    panel._current_path = source
    monkeypatch.setattr(
        "gui_qt.panels.mediainfo_panel.toast.show_error",
        lambda *_args, **_kwargs: None)
    panel._on_error(panel._request_serial, "broken")
    assert "interview-final.mp4" in panel.result_hint.text()


def test_details_table_is_accessible_and_narrow_layout_has_no_overflow(panel, app):
    panel.resize(640, 820)
    panel.show()
    app.processEvents()

    assert panel.table.accessibleName()
    assert panel.horizontalScrollBar().maximum() == 0
