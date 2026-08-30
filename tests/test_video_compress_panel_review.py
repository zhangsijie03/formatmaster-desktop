"""视频压缩页局部优化：参数说明与真实批次大小状态。"""
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
    from gui_qt.panels.video_compress_panel import VideoCompressPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    panel = VideoCompressPanelPage(object(), services)
    monkeypatch.setattr(panel, "_schedule_prefs_save", lambda: None)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    monkeypatch.setattr(services, "ffmpeg_ready", lambda: True)
    panel.cb_quality.setCurrentIndex(1)
    panel.cb_res.setCurrentIndex(0)
    panel.cb_codec.setCurrentIndex(0)
    yield app, panel
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_compression_parameter_hints_and_contract(compress_panel):
    from core.video_compress import CRF_PRESETS, RES_VALUES
    from gui_qt.panels.video_compress_panel import QUALITY_KEYS, QUALITY_HINTS, RES_KEYS, CODEC_KEYS

    _app, panel = compress_panel
    for quality_index, quality in enumerate(QUALITY_KEYS):
        panel.cb_quality.setCurrentIndex(quality_index)
        assert QUALITY_HINTS[quality] in panel.settings_hint.text()
        for resolution_index, resolution in enumerate(RES_KEYS):
            panel.cb_res.setCurrentIndex(resolution_index)
            for codec_index, codec in enumerate(CODEC_KEYS):
                panel.cb_codec.setCurrentIndex(codec_index)
                params = panel.collect_params()
                assert params["crf"] == CRF_PRESETS[quality]
                assert params["max_height"] == RES_VALUES[resolution]
                assert params["codec"] == codec
                assert ("H.265" if codec_index == 0 else "H.264") in panel.settings_hint.text()
                if resolution_index:
                    assert str(RES_VALUES[resolution]) in panel.settings_hint.text()


def test_compression_source_selection_and_responsive_layout(compress_panel, tmp_path):
    app, panel = compress_panel
    panel.show()
    app.processEvents()
    assert panel.size_bar.isHidden()
    assert panel.result_hint.isHidden()
    assert not panel.btn_preview.isEnabled()
    first = tmp_path / "one.mp4"
    second = tmp_path / ("很长的文件名" * 10 + ".mp4")
    first.write_bytes(b"a" * 100)
    second.write_bytes(b"b" * 200)
    panel.file_card.add_files([str(first), str(second)])
    panel.file_card.table.selectRow(1)
    assert panel.preview_hint.toolTip() == str(second)
    assert second.name in panel.preview_hint.text()
    assert panel.size_bar._before == 300
    assert panel.size_bar._after == 0
    for width, columns in ((720, 1), (1280, 2)):
        panel.resize(width, 1100)
        app.processEvents()
        assert panel.params_grid._columns == columns
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
    panel.file_card.clear_files()
    assert panel.size_bar.isHidden()
    assert panel.result_hint.isHidden()
    assert not panel.btn_preview.isEnabled()


def test_compression_new_batch_clears_old_result_and_blocks_duplicates(compress_panel, tmp_path, monkeypatch):
    from gui_qt import task_manager as tm
    from gui_qt.i18n import tr

    _app, panel = compress_panel
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 1000)
    panel.file_card.add_files([str(source)])
    panel.size_bar.set_sizes(2000, 500)
    panel.out_row.resolve_dir = lambda path: str(tmp_path)
    tasks = []
    monkeypatch.setattr(panel.services.task_manager, "add_task", lambda **kwargs: tasks.append(kwargs) or 1)
    monkeypatch.setattr(panel.services.task_manager, "get_task", lambda task_id: SimpleNamespace(state=tm.RUNNING))
    assert panel._start() is True
    assert panel.size_bar._before == 1000
    assert panel.size_bar._after == 0
    assert tr("正在压缩", "Compressing") in panel.result_hint.text()
    assert panel._start() is False
    assert len(tasks) == 1
    assert tasks[0]["params"]["crf"] == 28
    assert tasks[0]["params"]["codec"] == "libx265"
    assert tasks[0]["output_path"] != str(source)


@pytest.mark.parametrize("output_size", [500, 1200])
def test_compression_completed_result_excludes_failed_files(compress_panel, tmp_path, monkeypatch, output_size):
    from gui_qt import task_manager as tm
    from gui_qt.i18n import tr

    _app, panel = compress_panel
    source = tmp_path / "source.mp4"
    failed = tmp_path / "failed.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"a" * 1000)
    failed.write_bytes(b"b" * 300)
    output.write_bytes(b"c" * output_size)
    panel.file_card.add_files([str(source), str(failed)])
    panel._task_rows = {1: (str(source), 0), 2: (str(failed), 1)}
    panel._batch_progress = {1: 0, 2: 0}
    tasks = {
        1: SimpleNamespace(state=tm.SUCCESS, file_path=str(source), output_path=str(output)),
        2: SimpleNamespace(state=tm.RUNNING, file_path=str(failed), error="test error"),
    }
    monkeypatch.setattr(panel.services.task_manager, "get_task", tasks.get)
    panel._on_state(1, tm.SUCCESS)
    assert panel.size_bar._after == 0  # 本批结束前不把部分结果当作完整结果
    tasks[2].state = tm.FAILED
    panel._on_state(2, tm.FAILED)
    assert panel.size_bar._before == 1000
    assert panel.size_bar._after == output_size
    assert "1" in panel.result_hint.text()
    assert tr("最近完成批次", "Last completed batch") in panel.result_hint.text()
    if output_size > 1000:
        assert tr("未减小体积", "Size did not decrease") in panel.result_hint.text()


def test_compression_no_successful_sizes_do_not_show_pending_result(compress_panel, tmp_path, monkeypatch):
    from gui_qt import task_manager as tm
    from gui_qt.i18n import tr

    _app, panel = compress_panel
    source = tmp_path / "source.mp4"
    source.write_bytes(b"a" * 100)
    panel.file_card.add_files([str(source)])
    panel._task_rows = {1: (str(source), 0)}
    task = SimpleNamespace(state=tm.CANCELLED, file_path=str(source))
    monkeypatch.setattr(panel.services.task_manager, "get_task", lambda task_id: task)
    panel._on_state(1, tm.CANCELLED)
    assert panel.size_bar.isHidden()
    assert tr("暂无可比较", "No successful sizes") in panel.result_hint.text()
