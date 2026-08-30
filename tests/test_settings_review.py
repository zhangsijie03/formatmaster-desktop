"""设置菜单顺序审查的关键回归测试。"""

import inspect
import json
import zipfile

import pytest

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)


def test_backup_validates_every_json_before_overwriting(tmp_path, monkeypatch):
    """后置损坏条目不能造成前置文件已被覆盖的半恢复状态。"""
    from utils import backup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    current = data_dir / "prefs.json"
    current.write_text('{"theme": "current"}', encoding="utf-8")
    archive = tmp_path / "broken.zip"
    _write_zip(archive, [
        ("prefs.json", '{"theme": "backup"}'),
        ("history.json", "not-json"),
    ])
    monkeypatch.setattr(backup, "get_user_data_dir", lambda: str(data_dir))

    with pytest.raises(json.JSONDecodeError):
        backup.import_backup(str(archive))

    assert current.read_text(encoding="utf-8") == '{"theme": "current"}'
    assert not (data_dir / "history.json").exists()


def test_backup_rejects_nested_json_paths(tmp_path, monkeypatch):
    """备份只接受根目录文件，拒绝目录穿越和伪造嵌套路径。"""
    from utils import backup

    data_dir = tmp_path / "data"
    archive = tmp_path / "unsafe.zip"
    _write_zip(archive, [("../prefs.json", "{}")])
    monkeypatch.setattr(backup, "get_user_data_dir", lambda: str(data_dir))

    with pytest.raises(ValueError, match="不安全"):
        backup.import_backup(str(archive))
    assert not data_dir.exists()


def test_backup_imports_valid_json_atomically(tmp_path, monkeypatch):
    from utils import backup

    data_dir = tmp_path / "data"
    archive = tmp_path / "valid.zip"
    _write_zip(archive, [
        ("prefs.json", '{"theme": "dark"}'),
        ("history.json", "[]"),
    ])
    monkeypatch.setattr(backup, "get_user_data_dir", lambda: str(data_dir))

    assert backup.import_backup(str(archive)) == 2
    assert json.loads((data_dir / "prefs.json").read_text()) == {
        "theme": "dark"}
    assert not list(data_dir.glob(".backup_import_*"))


def test_ffmpeg_validation_uses_managed_worker():
    """设置页不得回退到直接创建 threading.Thread。"""
    from gui_qt.components.safe_worker import SafeWorker
    from gui_qt.pages import settings_page

    assert issubclass(settings_page._FfmpegValidationWorker, SafeWorker)
    source = inspect.getsource(settings_page.SettingsPage._browse_ffmpeg)
    assert "threading.Thread" not in source
    assert "_FfmpegValidationWorker" in source


def test_destructive_settings_actions_require_confirmation():
    from gui_qt.pages.settings_page import SettingsPage

    for method_name in ("_delete_preset", "_import_backup", "_clear_log"):
        source = inspect.getsource(getattr(SettingsPage, method_name))
        assert "_confirm_destructive" in source


def test_proxy_fields_follow_mode_and_validate_port(app, monkeypatch):
    from PySide6.QtGui import QValidator
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.pages.settings_page import SettingsPage
    from gui_qt.services import QtServices

    # 页面切换代理会修改进程环境；测试结束必须恢复，不能污染后续 LAN 用例。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)

    prefs = {"proxy_mode": "off", "proxy_host": "127.0.0.1",
             "proxy_port": 7890}
    services = QtServices()
    services.get_pref = lambda key, default=None: prefs.get(key, default)
    services.set_pref = lambda key, value: prefs.__setitem__(key, value)
    services.theme_mgr = ThemeManager(services)

    class _Window:
        pages = {}

    page = SettingsPage(_Window(), services)
    try:
        page.pivot.setCurrentRow(page._section_order.index("network"))
        app.processEvents()
        assert not page.proxy_host_edit.isEnabled()
        assert not page.proxy_port_edit.isEnabled()
        page.card_proxy_mode.comboBox.setCurrentIndex(1)
        assert page.proxy_host_edit.isEnabled()
        assert page.proxy_port_edit.isEnabled()
        validator = page.proxy_port_edit.validator()
        assert validator.validate("65535", 5)[0] == QValidator.Acceptable
        assert validator.validate("65536", 5)[0] != QValidator.Acceptable
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
