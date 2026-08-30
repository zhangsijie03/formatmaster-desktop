"""证件照页局部体验：处理说明、结果反馈与响应式布局。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture
def id_photo_panel(monkeypatch):
    from gui_qt.panels.id_photo_panel import IdPhotoPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = IdPhotoPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    # 这些测试只审查页面反馈，不运行真实抠图线程。
    monkeypatch.setattr(panel, "_preview_file", lambda *_args: None)
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_page_explains_batch_preview_and_compliance_scope(id_photo_panel):
    _app, panel = id_photo_panel
    assert panel.input_hint.wordWrap()
    assert "预览" in panel.input_hint.text() or "Preview" in panel.input_hint.text()
    assert panel.preview_hint.wordWrap()
    assert "办理机构" in panel.preview_hint.text() or "authority" in panel.preview_hint.text()


def test_engine_explanation_tracks_ai_choice(id_photo_panel):
    _app, panel = id_photo_panel
    panel.cb_ai.setChecked(False)
    assert "色度键" in panel.engine_hint.text() or "Chroma key" in panel.engine_hint.text()
    panel.cb_ai.setChecked(True)
    assert "AI" in panel.engine_hint.text()
    assert panel.collect_params()["use_ai"] is True


def test_output_summary_tracks_batch_format_and_print_mode(
        id_photo_panel, tmp_path):
    _app, panel = id_photo_panel
    sources = [tmp_path / "one.jpg", tmp_path / "two.png"]
    for source in sources:
        Image.new("RGB", (30, 40), "white").save(source)
    panel.file_card.add_files([str(source) for source in sources])
    assert "2" in panel.output_hint.text()
    assert "JPG" in panel.output_hint.text()

    panel.cb_bg.setCurrentIndex(6)
    assert "PNG" in panel.output_hint.text()
    panel.cb_print.setChecked(True)
    assert ("排版" in panel.output_hint.text()
            or "layout" in panel.output_hint.text())
    assert ("冲印" in panel.action_bar.btn_go.text()
            or "layout" in panel.action_bar.btn_go.text())


def test_target_summary_uses_visible_size_and_color(id_photo_panel):
    _app, panel = id_photo_panel
    panel.cb_size.setCurrentIndex(2)
    panel.cb_bg.setCurrentIndex(1)
    assert panel.cb_size.currentText() in panel.file_card._fmt_text
    assert panel.cb_bg.currentText() in panel.file_card._fmt_text
    assert "/" in panel.file_card._fmt_text


def test_responsive_reflow_preserves_current_settings(id_photo_panel):
    app, panel = id_photo_panel
    panel.cb_size.setCurrentIndex(4)
    panel.cb_bg.setCurrentIndex(2)
    expected = panel.collect_params()
    panel.show()
    for width, columns in ((720, 1), (1180, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.settings_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.collect_params() == expected
        assert panel.output_hint.wordWrap()
