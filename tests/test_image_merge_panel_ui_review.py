"""图片合并页局部体验：模式语义、顺序与单文件输出反馈。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture
def merge_panel(monkeypatch):
    from gui_qt.panels.image_merge_panel import ImageMergePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = ImageMergePanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_merge_modes_explain_real_scaling_rules(merge_panel):
    _app, panel = merge_panel
    panel.cb_mode.setCurrentIndex(0)
    assert "最大宽度" in panel.mode_hint.text() or "widest" in panel.mode_hint.text()
    assert "白色" in panel.mode_hint.text() or "white" in panel.mode_hint.text()
    panel.cb_mode.setCurrentIndex(1)
    assert "最大高度" in panel.mode_hint.text() or "tallest" in panel.mode_hint.text()
    assert panel.cb_gap.isEnabled()


def test_pdf_modes_explain_page_behavior_and_disable_gap(merge_panel):
    _app, panel = merge_panel
    panel.cb_mode.setCurrentIndex(2)
    assert "A4" in panel.mode_hint.text()
    assert not panel.cb_gap.isEnabled()
    panel.cb_mode.setCurrentIndex(3)
    assert ("原始像素" in panel.mode_hint.text()
            or "original pixel" in panel.mode_hint.text())
    assert "PDF" in panel.action_bar.btn_go.text()


def test_output_summary_tracks_first_file_and_single_result(
        merge_panel, tmp_path):
    _app, panel = merge_panel
    files = [tmp_path / "cover.png", tmp_path / "detail.jpg"]
    for path in files:
        Image.new("RGB", (30, 20), "white").save(path)
    panel.file_card.add_files([str(path) for path in files])
    assert "2" in panel.output_hint.text()
    assert "cover_merged.jpg" in panel.output_hint.text()
    panel.cb_mode.setCurrentIndex(2)
    assert "cover_merged.pdf" in panel.output_hint.text()
    assert "1" in panel.output_hint.text()


def test_order_hint_covers_image_and_pdf_outputs(merge_panel):
    _app, panel = merge_panel
    assert panel.order_hint.wordWrap()
    assert "长图" in panel.order_hint.text() or "Long images" in panel.order_hint.text()
    assert "PDF" in panel.order_hint.text()


def test_responsive_layout_keeps_settings_and_no_overflow(merge_panel):
    app, panel = merge_panel
    panel.cb_mode.setCurrentIndex(1)
    panel.cb_gap.setCurrentText("20")
    expected = panel.collect_params()
    panel.show()
    for width, columns in ((700, 1), (1120, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.settings_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.collect_params() == expected
        assert panel.mode_hint.wordWrap()
        assert panel.output_hint.wordWrap()
