"""批量重命名页的规则说明、预览状态和响应式回归测试。"""

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
    page.save_prefs = lambda: None
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _files(tmp_path, *names):
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text("fixture", encoding="utf-8")
        paths.append(str(path))
    return paths


def test_rule_order_and_extension_behavior_are_explicit(panel):
    text = panel.rule_order_hint.text().lower()
    assert "{ext}" in text
    assert "执行顺序" in text or "order:" in text
    assert "正则" in text or "regex" in text


def test_preview_actions_use_existing_header_action_area(panel):
    assert panel.btn_preview.parent() is panel.header
    assert panel.btn_go.parent() is panel.header


def test_preview_updates_file_states_and_warns_about_source_names(
        panel, tmp_path):
    panel.file_card.add_files(_files(tmp_path, "first.txt", "second.txt"))
    panel.ed_pattern.setText("item_{n:02d}")

    panel._preview()

    assert panel.file_card.table.item(0, 3).text() in ("将重命名", "Will rename")
    assert "源文件名" in panel.preview_status.text() or "source filenames" in panel.preview_status.text()
    assert panel.btn_go.isEnabled()


def test_rule_change_invalidates_preview_and_file_states(panel, tmp_path):
    panel.file_card.add_files(_files(tmp_path, "source.txt"))
    panel.ed_pattern.setText("renamed_{n}")
    panel._preview()

    panel.ed_pattern.setText("changed_{n}")

    assert "失效" in panel.preview_status.text() or "no longer valid" in panel.preview_status.text()
    assert panel.file_card.table.item(0, 3).text() in ("待预览", "Needs preview")
    assert not panel.btn_go.isEnabled()


def test_preview_table_is_accessible_and_640px_has_no_overflow(
        panel, app, tmp_path):
    panel.file_card.add_files(_files(tmp_path, "one.jpg", "two.jpg"))
    panel.resize(640, 820)
    panel.show()
    app.processEvents()

    assert panel.preview_table.accessibleName()
    assert panel.rule_grid._columns == 1
    assert panel.btn_preview.y() == panel.btn_go.y()
    assert panel.horizontalScrollBar().maximum() == 0
