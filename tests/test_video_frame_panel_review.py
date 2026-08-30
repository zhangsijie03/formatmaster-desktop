"""抽帧页局部交互：密度、缩略图规格、手动入口和批量契约。"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def frame_panel(monkeypatch):
    from gui_qt.panels.video_frame_panel import VideoFramePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = VideoFramePanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"mode": "frames", "interval": "1", "fmt": "PNG", "cols": "4", "rows": "4", "width": "1600"})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_frame_density_and_output_summary(frame_panel, tmp_path):
    from gui_qt.panels.video_frame_panel import INTERVAL_VALUES

    _app, panel = frame_panel
    for interval in INTERVAL_VALUES:
        panel.cb_interval.setCurrentText(interval)
        assert f"{60 / float(interval):g}" in panel.frames_hint.text()
        assert panel.collect_params()["interval"] == interval
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    panel.file_card.add_files([str(video)])
    panel.cb_fmt.setCurrentText("JPG")
    assert "1" in panel.output_hint.text()
    assert "frame_00000.jpg" in panel.output_hint.text()
    assert "JPG" in panel.file_card._fmt_text
    panel.file_card.clear_files()
    assert "0" in panel.output_hint.text()
    assert not panel.btn_picker.isEnabled()


def test_sheet_summary_matches_engine_width_alignment(frame_panel):
    from gui_qt.panels.video_frame_panel import COLS_VALUES, WIDTH_VALUES

    _app, panel = frame_panel
    panel.sg_mode.setCurrentItem("sheet")
    panel.cb_rows.setCurrentText("3")
    for cols in COLS_VALUES:
        panel.cb_cols.setCurrentText(cols)
        for width in WIDTH_VALUES:
            panel.cb_width.setCurrentText(width)
            aligned = int(width) // int(cols) * int(cols)
            assert str(int(cols) * 3) in panel.sheet_hint.text()
            assert str(aligned) in panel.sheet_hint.text()
            assert f"{aligned} px" in panel.file_card._fmt_text
            # UI 解释引擎的整除行为，不更改原有任务入参。
            assert panel.collect_params()["width"] == width
    assert "thumbnails.png" in panel.output_hint.text()


def test_frame_mode_switch_preserves_preferences_and_fits_window(frame_panel):
    from gui_qt.i18n import tr

    app, panel = frame_panel
    panel.apply_prefs({"interval": "5", "fmt": "JPG", "cols": "6", "rows": "3", "width": "800"})
    panel.show()
    for width, frame_columns, sheet_columns in ((720, 1, 1), (1280, 2, 3)):
        panel.resize(width, 1100)
        for mode in ("frames", "sheet"):
            panel.sg_mode.setCurrentItem(mode)
            app.processEvents()
            assert panel.card_frames.isVisible() == (mode == "frames")
            assert panel.card_sheet.isVisible() == (mode == "sheet")
            assert panel.frames_grid._columns == frame_columns
            assert panel.sheet_grid._columns == sheet_columns
            assert panel.horizontalScrollBar().maximum() == 0
            assert panel.widget().width() <= panel.viewport().width()
            assert panel.action_bar.btn_go.text() == (tr("开始抽帧", "Extract frames") if mode == "frames" else tr("生成缩略图墙", "Generate sheets"))
    saved = panel.collect_prefs()
    assert saved["interval"] == "5"
    assert saved["fmt"] == "JPG"
    assert saved["cols"] == "6"
    assert saved["rows"] == "3"
    assert saved["width"] == "800"


def test_frame_picker_tracks_selection_and_requires_output_folder(frame_panel, tmp_path, monkeypatch):
    import gui_qt.components.toast as toast

    _app, panel = frame_panel
    first, second = tmp_path / "one.mp4", tmp_path / "two.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    panel.file_card.add_files([str(first), str(second)])
    panel.file_card.table.selectRow(1)
    assert second.name in panel.picker_hint.text()
    assert panel.picker_hint.toolTip() == str(second)
    opened, warnings = [], []

    class Picker:
        def __init__(self, path, out_dir, parent):
            opened.append((path, out_dir))

        def exec(self):
            pass

    monkeypatch.setitem(sys.modules, "gui_qt.components.frame_picker", SimpleNamespace(FramePickerDialog=Picker))
    monkeypatch.setattr(toast, "show_warning", lambda parent, text: warnings.append(text))
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, "")
    panel._open_picker()
    assert warnings
    assert not opened
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(tmp_path))
    params = panel.collect_params()
    panel._open_picker()
    assert opened == [(str(second), str(tmp_path))]
    assert panel.collect_params() == params


def test_frame_runner_keeps_sequence_and_sheet_contracts(frame_panel, monkeypatch):
    import core.video_frame_extract as extract
    import core.thumbnail_sheet as sheet

    _app, panel = frame_panel
    calls = []
    monkeypatch.setattr(extract, "extract_frames", lambda *args: calls.append(args) or (True, 5))
    monkeypatch.setattr(sheet, "generate_thumbnail_sheet", lambda *args: calls.append(args) or True)
    panel.apply_prefs({"mode": "frames", "interval": "2", "fmt": "JPG"})
    task = SimpleNamespace(file_path="video.mp4", output_path="out/video_frames/frame_00000.jpg", params=panel.collect_params())
    assert panel._runner(task, None)
    assert calls[-1] == ("video.mp4", "out/video_frames", 2.0, "JPG", None)
    panel.apply_prefs({"mode": "sheet", "cols": "6", "rows": "3", "width": "800"})
    task.params = panel.collect_params()
    task.output_path = "out/video_thumbnails.png"
    assert panel._runner(task, None)
    assert calls[-1] == ("video.mp4", "out/video_thumbnails.png", 6, 3, 800, None)
