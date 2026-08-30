"""设置页局部交互回归；偏好、预设与系统行为均使用替身。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QWidget


class MemoryPresets:
    def __init__(self):
        self.data = {}
        self.saved = []
        self.deleted = []

    def list(self):
        return list(self.data)

    def save(self, name, panels):
        self.saved.append(name)
        self.data[name] = deepcopy(panels)

    def load(self, name):
        return deepcopy(self.data.get(name))

    def delete(self, name):
        self.deleted.append(name)
        self.data.pop(name, None)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app, monkeypatch):
    from gui_qt.components import toast
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.pages import settings_page
    from gui_qt.services import QtServices

    prefs = {"check_update_on_start": False, "update_check_freq": "weekly"}
    services = QtServices()
    services.get_pref = lambda key, default=None: prefs.get(key, default)
    services.set_pref = lambda key, value: prefs.__setitem__(key, value)
    services.theme_mgr = ThemeManager(services)
    monkeypatch.setattr(settings_page, "_autostart_enabled", lambda: False)
    monkeypatch.setattr(settings_page, "_set_autostart", lambda *args: pytest.fail("system setting changed"))
    monkeypatch.setattr(settings_page, "_make_preset_store", MemoryPresets)
    for name in ("show_info", "show_warning", "show_success", "show_error"):
        monkeypatch.setattr(toast, name, lambda *args, **kwargs: None)
    window = QWidget()
    window.pages = {}
    panel = settings_page.SettingsPage(window, services)
    panel.resize(1100, 900)
    panel.show()
    app.processEvents()
    yield panel
    panel.close()
    panel.deleteLater()
    window.deleteLater()
    app.processEvents()


def open_presets(page):
    page.section_tabs.setCurrentTab("presets")


def test_narrow_picker_reaches_hidden_sections_without_eager_build(page, app):
    assert page._built_sections == {"general"}
    page.resize(640, 900)
    app.processEvents()
    assert page.section_picker.isVisibleTo(page)
    assert page.section_picker.count() == len(page._section_order)
    page.section_picker.setCurrentIndex(page._section_order.index("shortcuts"))
    assert page.section_tabs.currentTab().routeKey() == "shortcuts"
    assert page.sg.currentWidget() is page._sections["shortcuts"][1]
    assert page._built_sections == {"general", "shortcuts"}
    page.section_tabs.setCurrentTab("general")
    assert page.section_picker.currentData() == "general"
    page.resize(1400, 900)
    app.processEvents()
    assert not page.section_picker.isVisibleTo(page)


def test_update_frequency_disabled_without_resetting_saved_value(page):
    assert not page.card_update_freq.isEnabled()
    assert page.card_update_freq.comboBox.currentIndex() == 2
    page._on_check_update_enabled(True)
    assert page.card_update_freq.isEnabled()
    page._on_check_update_enabled(False)
    assert not page.card_update_freq.isEnabled()
    assert page.services.get_pref("update_check_freq") == "weekly"


def test_empty_presets_disable_actions_and_refresh_preserves_choice(page):
    open_presets(page)
    assert not page.btn_preset_apply.isEnabled()
    assert not page.btn_preset_delete.isEnabled()
    assert not page.cb_preset.isEnabled()
    assert page.card_save_preset.isEnabled()
    page.preset_store.data = {"first": {"video": {}}, "second": {"video": {}}}
    page._reload_preset_list()
    page.cb_preset.setCurrentText("second")
    page._reload_preset_list()
    assert page.cb_preset.currentText() == "second"
    assert page.btn_preset_apply.isEnabled() and page.btn_preset_delete.isEnabled()


def test_save_selects_new_preset_and_blocks_dialog_reentry(page, monkeypatch):
    open_presets(page)
    page.main_window.pages = {"video": SimpleNamespace(
        panel_key="video", collect_prefs=lambda: {"format": "mp4"})}

    def name_dialog(*args):
        assert page._preset_busy and not page.card_save_preset.isEnabled()
        page._save_preset()
        page._delete_preset()
        return "new preset", True

    monkeypatch.setattr("gui_qt.pages.settings_page.QInputDialog.getText", name_dialog)
    page._save_preset()
    assert page.preset_store.saved == ["new preset"]
    assert page.cb_preset.currentText() == "new preset"
    assert not page._preset_busy and page.card_save_preset.isEnabled()


@pytest.mark.parametrize("confirmed", [False, True])
def test_same_name_overwrite_requires_confirmation(page, monkeypatch, confirmed):
    open_presets(page)
    page.preset_store.data = {"existing": {"video": {"format": "old"}}}
    page.main_window.pages = {"video": SimpleNamespace(
        panel_key="video", collect_prefs=lambda: {"format": "new"})}
    monkeypatch.setattr("gui_qt.pages.settings_page.QInputDialog.getText", lambda *args: ("existing", True))
    prompts = []
    monkeypatch.setattr(page, "_confirm_destructive", lambda *args: prompts.append(args) or confirmed)
    page._save_preset()
    assert len(prompts) == 1
    assert page.preset_store.data["existing"]["video"]["format"] == ("new" if confirmed else "old")
    assert not page._preset_busy


def test_empty_or_incomplete_snapshot_is_not_saved(page, monkeypatch):
    open_presets(page)
    monkeypatch.setattr("gui_qt.pages.settings_page.QInputDialog.getText", lambda *args: ("snapshot", True))
    page._save_preset()
    assert not page.preset_store.saved

    def fail():
        raise ValueError("unavailable panel")

    page.main_window.pages = {
        "video": SimpleNamespace(panel_key="video", collect_prefs=lambda: {"format": "mp4"}),
        "audio": SimpleNamespace(panel_key="audio", collect_prefs=fail),
    }
    page._save_preset()
    assert not page.preset_store.saved and not page._preset_busy


def test_delete_cancel_and_nested_actions_are_safe(page, monkeypatch):
    open_presets(page)
    page.preset_store.data = {"one": {"video": {}}}
    page._reload_preset_list()
    monkeypatch.setattr(page, "_confirm_destructive", lambda *args: False)
    page._delete_preset()
    assert not page.preset_store.deleted

    def confirm(*args):
        page._delete_preset()
        page._apply_preset()
        assert not page.btn_preset_delete.isEnabled()
        return True

    monkeypatch.setattr(page, "_confirm_destructive", confirm)
    page._delete_preset()
    assert page.preset_store.deleted == ["one"]
    assert not page.btn_preset_delete.isEnabled()
    assert not page._preset_busy and page.card_save_preset.isEnabled()


def test_apply_updates_panel_once_and_preserves_selection(page):
    open_presets(page)
    page.preset_store.data = {"one": {"video": {"format": "mp4"}}}
    page._reload_preset_list()
    applied = []

    def apply(params):
        page._apply_preset()
        applied.append(params)

    page.main_window.pages = {"video": SimpleNamespace(panel_key="video", apply_prefs=apply)}
    page._apply_preset()
    assert applied == [{"format": "mp4"}]
    assert page.cb_preset.currentText() == "one"
    assert not page._preset_busy


def test_storage_failure_restores_actions_and_reports_error(page, monkeypatch):
    from gui_qt.components import toast
    open_presets(page)
    page.preset_store.data = {"one": {"video": {}}}
    page._reload_preset_list()
    errors = []
    monkeypatch.setattr(toast, "show_error", lambda parent, message: errors.append(message))
    monkeypatch.setattr(page, "_confirm_destructive", lambda *args: True)

    def fail(name):
        raise OSError("read-only store")

    monkeypatch.setattr(page.preset_store, "delete", fail)
    page._delete_preset()
    assert errors and "read-only store" in errors[-1]
    assert "one" in page.preset_store.data
    assert not page._preset_busy and page.btn_preset_delete.isEnabled()


@pytest.mark.parametrize("width", [640, 820, 1100])
@pytest.mark.parametrize("section", ["general", "presets"])
def test_scoped_sections_fit_window(page, app, width, section):
    page.resize(width, 900)
    page.section_tabs.setCurrentTab(section)
    for _ in range(4):
        app.processEvents()
    assert page.horizontalScrollBar().maximum() == 0
    assert page.sg.currentWidget().horizontalScrollBar().maximum() == 0
