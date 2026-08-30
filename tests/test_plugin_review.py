"""插件中心审查回归：导入安全、原子性、覆盖顺序与键盘可达性。"""

import os
import sys
import zipfile
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QBoxLayout


PLUGIN_SOURCE = "PLUGIN_INFO = {'name': 'Review plugin'}\n"


@pytest.fixture(scope="module")
def app_ctx():
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)

    class Window:
        pass

    yield app, Window(), services
    app.processEvents()


def _write(path, content=PLUGIN_SOURCE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_validation_is_static_and_does_not_execute_plugin(tmp_path):
    from core.plugin_loader import validate_plugin_file

    marker = tmp_path / "executed.txt"
    plugin = _write(
        tmp_path / "safe.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
        "PLUGIN_INFO = {'name': 'Static check'}\n",
    )

    assert validate_plugin_file(plugin) == (True, "Static check")
    assert not marker.exists()


def test_validation_rejects_dynamic_metadata_symlink_and_large_file(tmp_path):
    from core.plugin_loader import MAX_PLUGIN_FILE_BYTES, validate_plugin_file

    dynamic = _write(tmp_path / "dynamic.py", "PLUGIN_INFO = dict(name='x')\n")
    assert not validate_plugin_file(dynamic)[0]

    link = tmp_path / "linked.py"
    link.symlink_to(_write(tmp_path / "real.py"))
    assert not validate_plugin_file(link)[0]

    large = tmp_path / "large.py"
    large.write_bytes(b"#" * (MAX_PLUGIN_FILE_BYTES + 1))
    assert not validate_plugin_file(large)[0]


def test_import_rejects_existing_target_without_overwrite(tmp_path):
    from core.plugin_loader import import_plugin

    source = _write(tmp_path / "source" / "same.py")
    target = tmp_path / "target"
    target.mkdir()
    existing = _write(target / "same.py", "PLUGIN_INFO = {'name': 'Old'}\n")

    ok, message = import_plugin(source, target)

    assert not ok and "已存在" in message
    assert "Old" in existing.read_text(encoding="utf-8")


def test_multi_file_import_rolls_back_if_commit_fails(tmp_path, monkeypatch):
    from core import plugin_loader

    source = tmp_path / "source"
    source.mkdir()
    _write(source / "one.py", "PLUGIN_INFO = {'name': 'One'}\n")
    _write(source / "two.py", "PLUGIN_INFO = {'name': 'Two'}\n")
    target = tmp_path / "target"
    real_replace = os.replace
    calls = 0

    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        real_replace(src, dst)

    monkeypatch.setattr(plugin_loader.os, "replace", fail_second)
    ok, message = plugin_loader.import_plugin(source, target)

    assert not ok and "simulated commit failure" in message
    assert list(target.iterdir()) == []


def test_zip_import_rejects_duplicate_basenames_and_oversize(tmp_path):
    from core.plugin_loader import MAX_PLUGIN_FILE_BYTES, import_plugin

    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("a/tool.py", PLUGIN_SOURCE)
        archive.writestr("b/tool.py", PLUGIN_SOURCE)
    ok, message = import_plugin(duplicate, tmp_path / "target-a")
    assert not ok and "重名" in message

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.py", b"#" * (MAX_PLUGIN_FILE_BYTES + 1))
    ok, message = import_plugin(oversized, tmp_path / "target-b")
    assert not ok and "安全限制" in message


def test_scan_user_plugin_shadows_same_named_builtin(tmp_path, monkeypatch):
    from core import plugin_loader

    user_dir = tmp_path / "user"
    built_in_dir = tmp_path / "builtin"
    user_dir.mkdir()
    built_in_dir.mkdir()
    _write(user_dir / "tool.py", "PLUGIN_INFO = {'name': 'User version'}\n")
    _write(built_in_dir / "tool.py", "PLUGIN_INFO = {'name': 'Built-in'}\n")
    monkeypatch.setattr(plugin_loader, "plugin_dirs",
                        lambda: [str(user_dir), str(built_in_dir)])

    plugins = plugin_loader.scan_plugins()

    assert [(item.name, item.source) for item in plugins] == [
        ("User version", str(user_dir / "tool.py"))]


def test_scan_reports_runtime_load_failures(tmp_path, monkeypatch):
    from core import plugin_loader

    _write(tmp_path / "broken.py", "raise RuntimeError('broken plugin')\n"
           "PLUGIN_INFO = {'name': 'Broken'}\n")
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [str(tmp_path)])

    plugins, errors = plugin_loader.scan_plugins(include_errors=True)

    assert plugins == []
    assert errors == ["broken.py"]
    assert "fm_plugin_broken" not in sys.modules


def test_plugin_card_has_keyboard_open_and_accessible_text(app_ctx):
    from gui_qt.panels.plugin_panel import _PluginCard

    app, _win, _services = app_ctx
    opened = []
    card = _PluginCard(7, "键盘插件", "可由键盘打开")
    card.clicked.connect(opened.append)
    card.show()
    card.setFocus()
    QTest.keyClick(card, Qt.Key_Return)
    app.processEvents()

    assert opened == [7]
    assert card.accessibleName() == "键盘插件"
    assert card.accessibleDescription() == "可由键盘打开"
    card.close()
    card.deleteLater()


def test_plugin_panel_empty_state_and_narrow_layout(app_ctx, monkeypatch):
    from core import plugin_loader
    from gui_qt.panels.plugin_panel import PluginPanelPage

    app, win, services = app_ctx
    monkeypatch.setattr(
        plugin_loader, "scan_plugins",
        lambda include_errors=False: ([], []) if include_errors else [])
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [])
    page = PluginPanelPage(win, services)
    page.resize(700, 700)
    page.show()
    app.processEvents()

    assert page.lb_empty.isVisible()
    assert page.bar.direction() == QBoxLayout.TopToBottom
    assert page.search_edit.placeholderText().endswith("…")
    assert page.search_edit.accessibleName()
    page.close()
    page.deleteLater()


def test_plugin_grid_reflows_from_four_to_two_columns(app_ctx, monkeypatch):
    from core import plugin_loader
    from gui_qt.panels.plugin_panel import PluginPanelPage

    app, win, services = app_ctx
    plugins = [SimpleNamespace(name=f"Plugin {index}", description="Description",
                               panel_class=None, source=f"plugin_{index}.py")
               for index in range(8)]
    monkeypatch.setattr(
        plugin_loader, "scan_plugins",
        lambda include_errors=False: (plugins, []) if include_errors else plugins)
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [])
    page = PluginPanelPage(win, services)
    page.resize(1100, 800)
    page.show()
    app.processEvents()
    assert page.cards_grid.itemAtPosition(0, 3) is not None

    page.resize(700, 800)
    app.processEvents()
    assert page.cards_grid.itemAtPosition(0, 2) is None
    assert page.cards_grid.itemAtPosition(1, 0) is not None
    assert page.horizontalScrollBar().maximum() == 0
    page.close()
    page.deleteLater()


def test_delete_aborts_when_confirmation_cannot_open(app_ctx, tmp_path,
                                                     monkeypatch):
    import qfluentwidgets
    from core import plugin_loader
    from gui_qt.panels import plugin_panel

    app, win, services = app_ctx
    user_dir = tmp_path / "plugins"
    user_dir.mkdir()
    source = _write(user_dir / "keep.py")
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [str(user_dir)])
    monkeypatch.setattr(plugin_panel.toast, "show_error", lambda *_args: None)

    class BrokenMessageBox:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("dialog unavailable")

    monkeypatch.setattr(qfluentwidgets, "MessageBox", BrokenMessageBox)
    page = plugin_panel.PluginPanelPage(win, services)
    page._plugins = [SimpleNamespace(name="Keep", source=str(source))]
    page._delete_plugin(0)
    app.processEvents()

    assert source.exists()
    page.deleteLater()
