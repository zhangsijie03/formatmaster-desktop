"""视频反挤压页局部体验：模式差异、选中预览与输出契约。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def unwarp_panel(monkeypatch):
    from gui_qt.panels.video_unwarp_panel import VideoUnwarpPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = VideoUnwarpPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"ratio": "auto"})
    yield app, panel
    worker = getattr(panel, "_info_worker", None)
    if worker is not None and worker.isRunning():
        worker.wait(3000)
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_unwarp_auto_mode_explains_metadata_and_source_container(unwarp_panel):
    from gui_qt.i18n import tr

    _app, panel = unwarp_panel
    assert panel._target_ratio() == "auto"
    assert tr("元数据", "metadata").lower() in panel.mode_hint.text().lower()
    assert tr("保留源容器", "Source container") in panel.file_card._fmt_text
    assert tr("修复显示比例", "Fix display ratio") == panel.action_bar.btn_go.text()
    assert "0" in panel.output_hint.text()
    assert not panel.w_custom.isVisible()


def test_unwarp_manual_modes_explain_reencoding_and_preserve_values(unwarp_panel):
    from gui_qt.panels.video_unwarp_panel import _RATIO_KEYS

    _app, panel = unwarp_panel
    for ratio in ("16:9", "4:3", "9:16", "1:1"):
        panel.cb_ratio.setCurrentIndex(_RATIO_KEYS.index(ratio))
        assert panel._target_ratio() == ratio
        assert ratio in panel.mode_hint.text()
        assert "MP4" in panel.file_card._fmt_text
        assert ratio in panel.action_bar.btn_go.text()
    panel.apply_prefs({"ratio": "21:9"})
    assert panel._target_ratio() == "21:9"
    assert panel.sb_w.value() == 21
    assert panel.sb_h.value() == 9
    assert "21:9" in panel.mode_hint.text()
    panel.apply_prefs({"ratio": "auto"})
    panel.apply_prefs({"ratio": "21:9"})
    assert panel._target_ratio() == "21:9"


def test_unwarp_preview_tracks_selected_file(unwarp_panel, tmp_path, monkeypatch):
    _app, panel = unwarp_panel
    first, second = tmp_path / "one.mp4", tmp_path / "two.mkv"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    monkeypatch.setattr(panel, "_start_info_worker", lambda _path: None)
    panel.file_card.add_files([str(first), str(second)])
    panel.file_card.table.selectRow(1)
    assert second.name in panel.preview_hint.text()
    assert panel.preview_hint.toolTip() == str(second)
    assert panel.btn_preview.isEnabled()
    opened = []

    class Preview:
        def __init__(self, path, _parent):
            opened.append(path)

        def exec(self):
            pass

    monkeypatch.setitem(sys.modules, "gui_qt.components.video_preview",
                        SimpleNamespace(VideoPreviewDialog=Preview))
    panel._preview_video()
    assert opened == [str(second)]
    panel.file_card.clear_files()
    assert not panel.btn_preview.isEnabled()
    assert "0" in panel.output_hint.text()


def test_unwarp_batch_outputs_match_each_mode(unwarp_panel, tmp_path):
    from gui_qt.panels.video_unwarp_panel import _RATIO_KEYS

    _app, panel = unwarp_panel
    sources = [tmp_path / "movie.mkv", tmp_path / "clip.webm"]
    for source in sources:
        source.write_bytes(b"video")
    panel.out_row.resolve_dir = lambda _path: str(tmp_path)
    panel._reserved_output_paths = set()
    auto_tasks = [panel._make_task(str(source)) for source in sources]
    assert auto_tasks[0]["output_path"].endswith("movie_unwarped.mkv")
    assert auto_tasks[1]["output_path"].endswith("clip_unwarped.webm")
    assert all(task["params"]["ratio"] == "auto" for task in auto_tasks)
    panel.cb_ratio.setCurrentIndex(_RATIO_KEYS.index("16:9"))
    panel._reserved_output_paths = set()
    manual_tasks = [panel._make_task(str(source)) for source in sources]
    assert all(task["output_path"].endswith("_unwarped.mp4") for task in manual_tasks)
    assert all(task["params"]["ratio"] == "16:9" for task in manual_tasks)


def test_unwarp_runner_and_responsive_contract(unwarp_panel, monkeypatch):
    import core.video_unwarp as engine

    app, panel = unwarp_panel
    calls = []
    monkeypatch.setattr(engine, "fix_aspect", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    for ratio in ("auto", "4:3"):
        panel.apply_prefs({"ratio": ratio})
        task = SimpleNamespace(file_path="source.mkv", output_path="result.mkv",
                               params=panel.collect_params())
        assert panel._runner(task, None)
        assert calls[-1] == (("source.mkv", "result.mkv", ratio), {"progress_cb": None})
    expected = panel.collect_prefs()
    panel.show()
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.params_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.collect_prefs() == expected
