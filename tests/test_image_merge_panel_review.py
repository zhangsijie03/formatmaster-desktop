"""“图片拼接 / PDF 相册”页面与任务链路定向回归测试。"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.image_merge_panel import ImageMergePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = ImageMergePanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_page_uses_stable_modes_and_never_requires_ffmpeg(panel):
    assert panel.need_ffmpeg is False
    panel.apply_prefs({"mode": "PDF album (A4)", "gap": "20"})
    params = panel.collect_params()
    assert params["mode"] == "pdf_a4"
    assert params["gap"] == 20
    assert not panel.cb_gap.isEnabled()
    assert panel.action_bar.btn_go.text() in (
        "生成 PDF 相册", "Create PDF album")

    panel.apply_prefs({"mode": "横向拼接", "gap": 40})
    assert panel.collect_params()["mode"] == "horizontal"
    assert panel.cb_gap.isEnabled()
    panel.apply_prefs({"mode": []})
    assert panel.collect_params()["mode"] == "vertical"


def test_single_source_output_is_protected_and_conflicts_are_renamed(
        panel, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gui_qt.task_manager.USER_PREFS.get",
        lambda _section, _key, _default=None: "auto_rename")
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 20), "white").save(source)
    panel.cb_mode.setCurrentIndex(0)
    first = panel._make_task(str(source))
    assert first["output_path"].endswith("photo_merged.jpg")
    existing = tmp_path / "photo_merged.jpg"
    Image.new("RGB", (10, 10), "black").save(existing)
    second = panel._make_task(str(source))
    assert second["output_path"].endswith("photo_merged_1.jpg")
    assert first["runner_key"] == "image_merge"


def test_legacy_english_runner_mode_keeps_horizontal_semantics(panel,
                                                                monkeypatch):
    captured = []
    monkeypatch.setattr(
        "core.image_album.merge_horizontal",
        lambda files, output, gap, progress: captured.append(
            (files, output, gap)) or True)
    task = SimpleNamespace(
        file_path="first.png", output_path="result.jpg",
        params={"all_files": ["first.png", "second.png"],
                "mode": "Horizontal", "gap": "20"})
    assert panel._runner(task, lambda *_args: None)
    assert captured == [(["first.png", "second.png"], "result.jpg", 20)]


def test_composite_submission_tracks_one_task_and_blocks_duplicate(panel,
                                                                   monkeypatch,
                                                                   tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (20, 20), "white").save(first)
    Image.new("RGB", (20, 20), "black").save(second)
    panel.file_card.clear_files()
    panel.file_card.add_files([str(first), str(second)])
    captured = []
    monkeypatch.setattr(
        panel.services.task_manager, "add_task",
        lambda **kwargs: captured.append(kwargs) or 701)

    assert panel._submit_files()
    assert len(captured) == 1
    assert captured[0]["params"]["all_files"] == [str(first), str(second)]
    assert captured[0]["output_path"].endswith("first_merged.jpg")
    assert panel._batch_progress == {701: 0}

    monkeypatch.setattr(
        panel.services.task_manager, "get_task",
        lambda task_id: SimpleNamespace(state="processing") if task_id == 701 else None)
    assert panel._submit_files()
    assert len(captured) == 1

    from gui_qt import task_manager as tm
    panel._on_progress(701, 50, "merging", "")
    assert panel._batch_progress[701] == 50
    panel._on_state(701, tm.SUCCESS)
    assert not panel._task_rows
    assert not panel._merge_task_files
    assert panel.merge_grid.isEnabled()
    assert panel.file_card.table.item(0, 3).text() == tm.state_text(tm.SUCCESS)
    assert panel.file_card.table.item(1, 3).text() == tm.state_text(tm.SUCCESS)


def test_settings_reflow_on_narrow_window(panel, app):
    panel.resize(700, 700)
    panel.show()
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_gap))[0] == 3
    panel.resize(1100, 700)
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_gap))[0] == 1
