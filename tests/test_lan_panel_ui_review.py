"""局域网面板的接收目录、会话密码与网卡刷新回归，不启动真实服务。"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtWidgets import QApplication, QBoxLayout, QFileDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, monkeypatch, tmp_path):
    from core import lan_transfer
    from gui_qt.components import toast
    from gui_qt.panels.lan_transfer_panel import LanTransferPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    prefs = {"lan_recv_dir": str(tmp_path)}
    services = QtServices()
    monkeypatch.setattr(services, "get_pref", lambda key, default=None: prefs.get(key, default))
    monkeypatch.setattr(services, "set_pref", lambda key, value: prefs.update({key: value}))
    services.task_manager = TaskManager(services)
    monkeypatch.setattr(lan_transfer, "get_lan_ips", lambda: ["192.168.1.25"])
    for name in ("show_info", "show_success", "show_warning", "show_error"):
        monkeypatch.setattr(toast, name, lambda *args, **kwargs: None)
    page = LanTransferPanelPage(object(), services)
    yield page
    page._server = None
    page.close()
    page.deleteLater()
    app.processEvents()


def test_receive_folder_visible_and_selection_is_remembered(panel, tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    assert panel.lb_recv_dir.toolTip() == str(tmp_path)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(selected))
    panel._choose_recv_dir()
    assert panel._recv_dir == str(selected)
    assert panel.services.get_pref("lan_recv_dir") == str(selected)
    assert panel.lb_recv_dir.toolTip() == str(selected)


def test_receive_folder_cannot_change_while_running(panel, monkeypatch):
    before = panel._recv_dir
    panel._server = SimpleNamespace()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: pytest.fail("picker opened"))
    panel._choose_recv_dir()
    assert panel._recv_dir == before


def test_receive_folder_open_uses_displayed_path(panel, monkeypatch):
    from utils import platform_utils

    opened = []
    monkeypatch.setattr(platform_utils, "open_path", lambda path: opened.append(path) or True)
    panel._open_recv_dir()
    assert opened == [panel.lb_recv_dir.toolTip()]


def test_password_copy_does_not_reveal_or_change_secret(panel, monkeypatch):
    from PySide6.QtWidgets import QLineEdit

    copied = []
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    panel.ed_token.setText("test-password")
    panel._copy_token()
    assert copied == ["test-password"]
    assert panel.ed_token.echoMode() == QLineEdit.Password
    panel.ed_token.setText("x")
    panel._copy_token()
    assert copied == ["test-password"]
    assert panel.ed_token.text() == "x"


def test_password_rotation_requires_confirmation_for_running_service(panel, monkeypatch):
    from qfluentwidgets import MessageBox

    tokens = []
    panel._server = SimpleNamespace(
        is_running=lambda: True, port=8000, set_access_token=tokens.append)
    before = panel.ed_token.text()
    monkeypatch.setattr(MessageBox, "exec", lambda self: False)
    panel._regenerate_token()
    assert panel.ed_token.text() == before and not tokens
    monkeypatch.setattr(MessageBox, "exec", lambda self: True)
    panel._regenerate_token()
    assert tokens == [panel.ed_token.text()]
    assert len(tokens[0]) == 8
    assert tokens[0] not in panel._current_url
    assert panel.services.get_pref("lan_token") == ""


def test_refresh_ip_updates_live_qr_even_with_signals_blocked(panel, monkeypatch):
    from core import lan_transfer

    panel._server = SimpleNamespace(is_running=lambda: True, port=8000)
    monkeypatch.setattr(lan_transfer, "get_lan_ips", lambda: ["127.0.0.1", "10.0.0.5"])
    panel._refresh_ips()
    assert panel._selected_ip() == "10.0.0.5"
    assert panel._current_url == "http://10.0.0.5:8000/chat"
    assert panel._card_addr._value_label.text() == "10.0.0.5:8000"


def test_loopback_warning_and_manual_selection_update_while_idle(panel, monkeypatch):
    from core import lan_transfer
    from gui_qt.i18n import tr

    monkeypatch.setattr(lan_transfer, "get_lan_ips", lambda: ["127.0.0.1"])
    panel._refresh_ips()
    assert tr("手机无法", "phones cannot") in panel.tip_txt.text()
    panel.cb_ip.addItem("10.0.0.8")
    panel.cb_ip.setCurrentIndex(1)
    assert tr("可信网络", "trusted network") in panel.tip_txt.text()


def test_toggle_is_guarded_and_restores_button_after_failure(panel, monkeypatch):
    calls = []

    def start():
        calls.append("start")
        assert not panel.btn_toggle.isEnabled()
        panel._toggle()
        return False

    monkeypatch.setattr(panel, "_start_server", start)
    panel._toggle()
    assert calls == ["start"]
    assert panel.btn_toggle.isEnabled()


@pytest.mark.parametrize("width", [640, 700, 1000, 1180])
def test_running_layout_has_no_horizontal_overflow(panel, app, width):
    panel.resize(width, 900)
    panel._server = SimpleNamespace(port=8000, url="http://192.168.1.25:8000/")
    panel._show_ready("http://192.168.1.25:8000/chat")
    panel.show()
    for _ in range(3):
        app.processEvents()
    assert panel.horizontalScrollBar().maximum() == 0
    assert not panel.btn_choose_recv.isEnabled()
    if width < 820:
        assert panel._info_lay.direction() == QBoxLayout.TopToBottom
    assert panel.lb_recv_dir.toolTip() == panel._recv_dir
