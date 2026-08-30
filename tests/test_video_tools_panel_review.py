"""视频处理页局部优化：参数适用性、各模式布局和任务契约。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def video_panel(monkeypatch):
    from gui_qt.panels.video_edit_panel import VideoToolsPanelPage, _InfoWorker
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    # 交互测试只检查状态，不启动真实 ffprobe 或后台转换。
    monkeypatch.setattr(_InfoWorker, "start", lambda self: None)
    panel = VideoToolsPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    panel.apply_prefs({"category": "basic", "mode": "clip"})
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_video_tools_all_modes_fit_and_keep_input(video_panel):
    from gui_qt.panels.video_edit_panel import MODES, MODES2, MODE_HINTS

    app, panel = video_panel
    sections = {
        "clip": panel.w_clip, "merge": panel.w_merge, "subtitle": panel.w_sub,
        "speed": panel.w_speed, "delogo": panel.w_delogo,
        "reverse": panel.w_reverse, "gif": panel.w_gif,
        "watermark": panel.w_watermark, "stabilize": panel.w_stabilize,
        "track": panel.w_track,
    }
    panel.ed_start.setText("00:12")
    panel.ed_wm_text.setText("Demo")
    panel.show()
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 1100)
        for category, modes, selector in (("basic", MODES, panel.sg_mode),
                                          ("effects", MODES2, panel.sg_mode2)):
            panel.sg_category.setCurrentItem(category)
            for key, _label in modes:
                selector.setCurrentItem(key)
                app.processEvents()
                assert sections[key].isVisible()
                assert sum(section.isVisible() for section in sections.values()) == 1
                assert panel.mode_hint.text() == MODE_HINTS[key]
                assert panel.horizontalScrollBar().maximum() == 0
                assert panel.widget().width() <= panel.viewport().width()
                assert all(grid._columns == columns for grid in (
                    panel.speed_grid, panel.gif_grid, panel.watermark_grid, panel.track_grid))
    assert panel.ed_start.text() == "00:12"
    assert panel.ed_wm_text.text() == "Demo"


def test_video_tools_speed_controls_follow_supported_rates(video_panel):
    _app, panel = video_panel
    panel.sg_mode.setCurrentItem("speed")
    panel.cb_speed.setCurrentText("0.5x")
    panel.cb_interp.setChecked(True)
    assert panel.cb_interp.isEnabled()
    assert panel.collect_params()["interp"] is True
    panel.cb_speed.setCurrentText("2.0x")
    assert not panel.cb_interp.isEnabled()
    assert panel.collect_params()["interp"] is False
    assert panel.cb_interp.isChecked()  # 切回慢放时保留选项，不清空输入
    panel.cb_speed.setCurrentText("0.75x")
    assert panel.cb_interp.isEnabled()
    assert panel.collect_params()["interp"] is True
    assert panel.collect_params()["rate"] == 0.75


def test_video_tools_track_volume_only_enabled_for_mixing(video_panel):
    _app, panel = video_panel
    panel.apply_prefs({"category": "effects", "mode2": "track"})
    panel.sb_track_bg.setValue(0.6)
    panel.cb_track_mode.setCurrentIndex(0)
    assert not panel.sb_track_bg.isEnabled()
    assert panel.collect_params()["track_mode"] == 0
    panel.cb_track_mode.setCurrentIndex(1)
    assert panel.sb_track_bg.isEnabled()
    assert panel.collect_params()["track_bg"] == pytest.approx(0.6)
    assert panel.collect_params()["mode2"] == "track"
    assert panel.ed_track_audio.accessibleName()


def test_video_tools_context_commands_and_merge_constraint(video_panel, tmp_path):
    _app, panel = video_panel
    assert not panel.btn_clip_editor.isEnabled()
    assert not panel.btn_merge_preview.isEnabled()
    assert not panel.btn_edit_sub.isEnabled()
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    panel.sg_mode.setCurrentItem("merge")
    panel.file_card.add_files([str(first)])
    assert panel.btn_clip_editor.isEnabled()
    assert not panel.btn_merge_preview.isEnabled()
    assert not panel.action_bar.btn_go.isEnabled()
    panel.file_card.add_files([str(second)])
    assert panel.btn_merge_preview.isEnabled()
    assert panel.action_bar.btn_go.isEnabled()
    panel.file_card.remove_row(1)
    assert not panel.action_bar.btn_go.isEnabled()
    panel.sg_mode.setCurrentItem("subtitle")
    assert panel.action_bar.btn_go.isEnabled()
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    panel.ed_sub.setText(str(subtitle))
    assert panel.btn_edit_sub.isEnabled()
    panel.ed_sub.setText(str(tmp_path / "missing.srt"))
    assert not panel.btn_edit_sub.isEnabled()
    panel.ed_sub.setText(str(subtitle.with_suffix(".ass")))
    assert not panel.btn_edit_sub.isEnabled()
    panel.ed_sub.setText(str(subtitle))
    panel.file_card.clear_files()
    assert not panel.btn_edit_sub.isEnabled()
    assert not panel.btn_clip_editor.isEnabled()


def test_video_tools_runner_keeps_slow_motion_and_mix_contracts(video_panel, monkeypatch):
    from core import video_tools
    from types import SimpleNamespace

    _app, panel = video_panel
    calls = []
    monkeypatch.setattr(video_tools, "slowmo_interp", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(video_tools, "mix_audio", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    panel.sg_mode.setCurrentItem("speed")
    panel.cb_speed.setCurrentText("0.75x")
    panel.cb_interp.setChecked(True)
    task = SimpleNamespace(file_path="source.mp4", output_path="out.mp4", params=panel.collect_params())
    assert panel._runner(task, None)
    assert calls[-1][0] == ("source.mp4", "out.mp4", 0.75)
    assert calls[-1][1]["target_fps"] == 60
    panel.apply_prefs({"category": "effects", "mode2": "track"})
    panel.ed_track_audio.setText("music.wav")
    panel.cb_track_mode.setCurrentIndex(1)
    panel.sb_track_bg.setValue(0.6)
    task.params = panel.collect_params()
    assert panel._runner(task, None)
    assert calls[-1][0] == ("source.mp4", "music.wav", "out.mp4")
    assert calls[-1][1]["bg_volume"] == pytest.approx(0.6)
