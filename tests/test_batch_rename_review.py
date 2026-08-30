"""批量重命名计划安全、回滚、页面状态和响应式回归测试。"""

import os

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
    from gui_qt.panels.batch_rename_panel import BatchRenamePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = BatchRenamePanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _files(tmp_path, *names):
    paths = []
    for index, name in enumerate(names):
        path = tmp_path / name
        path.write_text(f"content-{index}", encoding="utf-8")
        paths.append(str(path))
    return paths


def test_plan_rejects_invalid_regex_placeholder_path_and_duplicate_target(
        tmp_path):
    from core.tools import build_rename_plan

    first, second = _files(tmp_path, "first.txt", "second.txt")
    with pytest.raises(ValueError):
        build_rename_plan([first], "item_{n}", regex_pattern="[")
    with pytest.raises(ValueError, match="占位符"):
        build_rename_plan([first], "item_{missing}")
    with pytest.raises(ValueError, match="路径分隔符"):
        build_rename_plan([first], "nested/item")
    with pytest.raises(FileExistsError, match="同一目标"):
        build_rename_plan([first, second], "same")


def test_existing_external_target_is_rejected_without_changing_files(tmp_path):
    from core.tools import batch_rename

    source, existing = _files(tmp_path, "source.txt", "taken.txt")
    with pytest.raises(FileExistsError, match="已存在"):
        batch_rename([source], "taken")

    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "content-0"
    assert (tmp_path / "taken.txt").read_text(encoding="utf-8") == "content-1"


def test_two_phase_execution_supports_swapping_names(tmp_path):
    from core.tools import execute_rename_plan

    first, second = _files(tmp_path, "a.txt", "b.txt")
    plan = [
        (first, "b.txt", second),
        (second, "a.txt", first),
    ]

    renamed = execute_rename_plan(plan)

    assert len(renamed) == 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "content-1"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "content-0"


def test_execution_failure_rolls_every_file_back(monkeypatch, tmp_path):
    from core import tools

    first, second = _files(tmp_path, "a.txt", "b.txt")
    plan = [
        (first, "b.txt", second),
        (second, "a.txt", first),
    ]
    real_rename = tools.os.rename
    calls = 0

    def fail_second_commit(source, target):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("simulated disk error")
        return real_rename(source, target)

    monkeypatch.setattr(tools.os, "rename", fail_second_commit)

    with pytest.raises(OSError, match="已恢复"):
        tools.execute_rename_plan(plan)

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "content-0"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "content-1"
    assert not list(tmp_path.glob("*.formatmaster-rename-*"))
    assert not list(tmp_path.glob(".*.formatmaster-rename-*"))


def test_panel_requires_valid_preview_and_invalidates_it_after_edit(
        panel, tmp_path):
    source = _files(tmp_path, "source.txt")[0]
    panel.file_card.add_files([source])
    panel.ed_pattern.setText("renamed_{n:02d}")
    panel._preview()

    assert panel.btn_go.isEnabled()
    assert panel.preview_table.item(0, 1).text() == "renamed_01.txt"

    panel.ed_pattern.setText("changed_{n}")

    assert not panel.btn_go.isEnabled()
    assert panel.preview_table.rowCount() == 0
    assert panel._preview_plan is None


def test_panel_executes_exact_preview_and_keeps_final_files_visible(
        panel, monkeypatch, tmp_path):
    source = _files(tmp_path, "source.txt")[0]
    panel.file_card.add_files([source])
    panel.ed_pattern.setText("renamed_{n}")
    panel._preview()
    monkeypatch.setattr(panel, "_confirm_run", lambda _count: True)
    monkeypatch.setattr(
        "gui_qt.panels.batch_rename_panel.toast.show_success",
        lambda *_args, **_kwargs: None)

    panel._run()

    target = tmp_path / "renamed_1.txt"
    assert target.is_file()
    assert panel.file_card.files() == [str(target)]
    assert panel.file_card.table.item(0, 3).text() in ("待预览", "Needs preview")
    assert not panel.btn_go.isEnabled()


def test_start_number_validation_and_stable_case_preference(panel, tmp_path):
    source = _files(tmp_path, "source.txt")[0]
    panel.file_card.add_files([source])
    panel.ed_start.setText("invalid")
    _plan, error = panel._plan()
    assert error and ("整数" in error or "integer" in error)

    panel.cb_case.setCurrentIndex(2)
    assert panel.collect_prefs()["case"] == "lower"
    panel.apply_prefs({"case": 1})
    assert panel.cb_case.currentIndex() == 1


def test_narrow_page_reflows_without_horizontal_scroll(panel, app):
    panel.resize(720, 760)
    panel.show()
    app.processEvents()

    assert panel.rule_grid._columns == 1
    assert panel.horizontalScrollBar().maximum() == 0

    panel.resize(1100, 760)
    app.processEvents()
    assert panel.rule_grid._columns == 2
