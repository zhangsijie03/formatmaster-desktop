"""插件中心的来源筛选、搜索恢复与可见错误反馈；不执行外部插件。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, monkeypatch, tmp_path):
    from core import plugin_loader
    from gui_qt.components import toast
    from gui_qt.i18n import current, set_language
    from gui_qt.panels.plugin_panel import PluginPanelPage
    from gui_qt.services import QtServices

    language = current()
    set_language("en")
    plugins = [
        plugin_loader.PluginInfo(name="JSON 格式化", description="JSON tool", source="json_formatter.py"),
        plugin_loader.PluginInfo(name="Imported note", description="Notebook", source="imported_note.py"),
        plugin_loader.PluginInfo(name="Long plugin name " * 12, description="Long description " * 10, source="long.py"),
    ]
    monkeypatch.setattr(plugin_loader, "scan_plugins", lambda **kwargs: (plugins, []))
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(PluginPanelPage, "_is_user_plugin", staticmethod(lambda source: source == "imported_note.py"))
    for name in ("show_info", "show_error", "show_success"):
        monkeypatch.setattr(toast, name, lambda *args: None)
    page = PluginPanelPage(object(), QtServices())
    yield page
    if page._errors_dialog:
        page._errors_dialog.reject()
    page.close()
    page.deleteLater()
    app.processEvents()
    set_language(language)


def visible(panel):
    return [card.idx for card in panel._cards if not card.isHidden()]


def test_source_filter_and_badges_use_stable_keys(panel):
    from gui_qt.panels.plugin_panel import PluginSource

    panel.source_filter.setCurrentIndex(2)
    assert panel.source_filter.currentData() == PluginSource.IMPORTED
    assert visible(panel) == [1]
    assert "1 / 3" in panel.lb_path.text()
    assert panel._cards[1].lb_source.text() == "Imported"
    assert panel._cards[0].btn_menu.isHidden()
    panel.source_filter.setCurrentIndex(1)
    assert visible(panel) == [0, 2]


@pytest.mark.parametrize("query", ["JSON", "格式化", "json_formatter.py"])
def test_search_matches_original_translated_name_and_filename(panel, query):
    panel.search_edit.setText(query)
    assert visible(panel) == [0]
    assert "1 / 3" in panel.lb_path.text()


def test_combined_empty_filter_can_be_cleared(panel):
    panel.source_filter.setCurrentIndex(2)
    panel.search_edit.setText("JSON")
    assert visible(panel) == []
    assert not panel.lb_empty.isHidden()
    assert not panel.btn_clear_filter.isHidden()
    assert "0 / 3" in panel.lb_path.text()
    panel._clear_filter()
    assert visible(panel) == [0, 1, 2]
    assert panel.btn_clear_filter.isHidden()


def test_rescan_keeps_current_source_and_search(panel):
    panel.source_filter.setCurrentIndex(2)
    panel.search_edit.setText("note")
    panel._scan()
    assert visible(panel) == [1]
    assert panel.search_edit.text() == "note"


def test_load_errors_have_plain_text_single_instance_details(panel, app, monkeypatch):
    from core import plugin_loader
    from qfluentwidgets import TextEdit

    failures = ["broken.py", "<b>untrusted.py</b>"]
    monkeypatch.setattr(plugin_loader, "scan_plugins", lambda **kwargs: ([], failures))
    panel._scan()
    assert not panel.btn_errors.isHidden()
    assert "2" in panel.btn_errors.text()
    panel._show_load_errors()
    dialog = panel._errors_dialog
    assert dialog.findChild(TextEdit).toPlainText() == "\n".join(failures)
    panel._show_load_errors()
    assert panel._errors_dialog is dialog
    dialog.reject()
    app.processEvents()
    assert panel._errors_dialog is None
    monkeypatch.setattr(plugin_loader, "scan_plugins", lambda **kwargs: ([], []))
    panel._scan()
    assert panel.btn_errors.isHidden()


def test_scan_reentry_is_ignored_and_failure_restores_actions(panel, monkeypatch):
    from core import plugin_loader

    calls = []
    before = panel._cards[:]

    def scan(**kwargs):
        calls.append(1)
        assert not panel.btn_scan.isEnabled()
        assert not panel.btn_import.isEnabled()
        panel._scan()
        raise OSError("unavailable")

    monkeypatch.setattr(plugin_loader, "scan_plugins", scan)
    panel._scan()
    assert calls == [1]
    assert panel._cards == before
    assert panel.btn_scan.isEnabled() and panel.btn_import.isEnabled()


@pytest.mark.parametrize("width", [640, 700, 1100])
def test_long_plugin_names_stay_within_grid(panel, app, width):
    panel.resize(width, 900)
    panel.show()
    for _ in range(3):
        app.processEvents()
    assert panel.horizontalScrollBar().maximum() == 0
    card = panel._cards[2]
    assert card.lb_name.toolTip() == "Long plugin name " * 12
    assert len(card.lb_name.text()) < len(card.lb_name.toolTip())
    assert card.accessibleName() == card.lb_name.toolTip()
