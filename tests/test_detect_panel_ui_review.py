"""格式检测页的信息层级、结果摘要与响应式回归测试。"""

import os
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
    page = DetectPanelPage(SimpleNamespace(pages={}), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _sample_results(tmp_path):
    regular = tmp_path / "photo.png"
    mismatch = tmp_path / "renamed.jpg"
    unknown = tmp_path / "archive.data"
    detected = {key: [] for key in (
        "video", "audio", "image", "doc", "pdf", "other")}
    detected["image"] = [str(regular), str(mismatch)]
    detected["other"] = [str(unknown)]
    info = [
        (str(regular), "image", "1.0 KB", "image", "image"),
        (str(mismatch), "image", "2.0 KB", "video", "image"),
        (str(unknown), "other", "3.0 KB", "other", None),
    ]
    return detected, info


def test_scan_scope_and_source_safety_are_explicit(panel):
    text = panel.scan_hint.text().lower()
    assert "子文件夹" in text or "subfolder" in text
    assert "不会修改" in text or "read-only" in text


def test_unroutable_file_rule_is_explained(panel):
    text = panel.routing_hint.text().lower()
    assert "格式不一致" in text or "mismatch" in text
    assert "未识别" in text or "unknown" in text


def test_result_summary_distinguishes_selected_review_and_unknown(
        panel, tmp_path):
    detected, info = _sample_results(tmp_path)
    panel._show_results(detected, info)

    summary = panel.result_summary.text()
    assert "3" in summary
    assert "1" in summary
    assert panel._result_review == 1
    assert panel._result_unknown == 1
    assert sum(chk.checkState() == Qt.Checked
               for _path, _cat, chk in panel._rows) == 1


def test_english_file_count_uses_singular_and_plural(monkeypatch):
    from gui_qt.panels import detect_panel

    monkeypatch.setattr(detect_panel, "tr", lambda _zh, en: en)

    assert detect_panel._file_count_text(1) == "1 file"
    assert detect_panel._file_count_text(2) == "2 files"


def test_result_summary_tracks_current_selection(panel, tmp_path):
    detected, info = _sample_results(tmp_path)
    panel._show_results(detected, info)
    selectable = next(chk for _path, _cat, chk in panel._rows
                      if chk.flags() & Qt.ItemIsUserCheckable)

    selectable.setCheckState(Qt.Unchecked)

    assert "已选 0" in panel.result_summary.text() or "0 convertible" in panel.result_summary.text()
    assert not panel.btn_go.isEnabled()


def test_page_preserves_accessibility_and_narrow_layout(panel, app):
    panel.resize(640, 820)
    panel.show()
    app.processEvents()

    assert panel.ed_path.accessibleName()
    assert panel.table.accessibleName()
    assert panel.horizontalScrollBar().maximum() == 0
