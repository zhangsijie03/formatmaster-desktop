"""图片压缩页局部体验：模式差异、输出说明和任务契约。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def compress_panel(monkeypatch):
    from gui_qt.panels.compress_img_panel import CompressImgPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = CompressImgPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"mode": "quality", "quality": "75",
                       "size_index": 0, "target_kb": 500})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_quality_mode_explains_format_and_resolution_behavior(compress_panel):
    _app, panel = compress_panel
    assert panel._mode == "quality"
    assert "JPEG/WebP" in panel.mode_hint.text()
    assert "PNG/TIFF" in panel.mode_hint.text()
    assert "75" in panel.mode_hint.text()
    panel.cb_q.setCurrentText("60")
    panel.cb_sz.setCurrentIndex(2)
    assert "60" in panel.mode_hint.text()
    assert "1280x720" in panel.mode_hint.text()
    assert "60" in panel.file_card._fmt_text
    assert "1280x720" in panel.file_card._fmt_text


def test_target_mode_explains_iteration_and_preserves_quality_settings(compress_panel):
    _app, panel = compress_panel
    panel.cb_q.setCurrentText("85")
    panel.cb_sz.setCurrentIndex(1)
    panel.seg_mode.setCurrentItem("target")
    panel.cb_target.setCurrentText("200")
    assert panel._mode == "target"
    assert "200" in panel.mode_hint.text()
    assert "JPEG/WebP" in panel.mode_hint.text()
    assert not panel.cb_q.isEnabled()
    assert not panel.cb_sz.isEnabled()
    assert panel.collect_params()["target_kb"] == 200
    panel.seg_mode.setCurrentItem("quality")
    assert panel.cb_q.currentText() == "85"
    assert panel.cb_sz.currentIndex() == 1
    panel._on_mode_changed("invalid")
    assert panel._mode == "quality"


def test_compress_batch_count_and_output_contract(compress_panel, tmp_path):
    _app, panel = compress_panel
    sources = [tmp_path / "one.png", tmp_path / "two.webp"]
    for source in sources:
        source.write_bytes(b"image")
    panel.file_card.add_files([str(source) for source in sources])
    assert "2" in panel.output_hint.text()
    panel.out_row.resolve_dir = lambda _path: str(tmp_path)
    panel._reserved_output_paths = set()
    tasks = [panel._make_task(str(source)) for source in sources]
    assert tasks[0]["output_path"].endswith("one_compressed.png")
    assert tasks[1]["output_path"].endswith("two_compressed.webp")
    assert all(not task["need_ffmpeg"] for task in tasks)
    panel.file_card.clear_files()
    assert "0" in panel.output_hint.text()


def test_compress_runners_keep_quality_and_target_contracts(compress_panel, monkeypatch):
    import core.tools as tools
    import gui_qt.panels.compress_img_panel as panel_module

    _app, panel = compress_panel
    calls = []
    monkeypatch.setattr(panel_module, "image_compress",
                        lambda *args: calls.append(("quality", args)) or True)
    monkeypatch.setattr(tools, "image_compress_to_size",
                        lambda *args: calls.append(("target", args)) or (True, "ok", 100))
    quality_task = SimpleNamespace(
        file_path="in.jpg", output_path="out.jpg", error="",
        params={"mode": "quality", "quality": 60, "max_size": (1280, 720),
                "target_kb": None})
    assert panel._runner(quality_task, None)
    assert calls[-1] == ("quality", ("in.jpg", "out.jpg", 60, (1280, 720), None))
    target_task = SimpleNamespace(
        file_path="in.png", output_path="out.png", error="",
        params={"mode": "target", "quality": 75, "max_size": None,
                "target_kb": 500})
    assert panel._runner(target_task, None)
    assert calls[-1] == ("target", ("in.png", "out.png", 500, None))


def test_compress_page_reflows_without_changing_preferences(compress_panel):
    app, panel = compress_panel
    panel.apply_prefs({"mode": "quality", "quality": "60",
                       "size_index": 2, "target_kb": 1024})
    expected = panel.collect_prefs()
    panel.show()
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.params_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
        assert panel.mode_hint.wordWrap()
        assert panel.output_hint.wordWrap()
