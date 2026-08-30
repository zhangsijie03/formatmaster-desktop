"""二维码页的内容说明、扫码反馈和响应式布局回归测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication, QBoxLayout


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.qrcode_panel import QrcodePanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = QrcodePanelPage(object(), services)
    page.save_prefs = lambda: None
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_url_hint_explains_protocol_handling(panel):
    panel.cb_type.setCurrentIndex(1)
    text = panel.type_hint.text().lower()
    assert "http" in text
    assert "https://" in text


def test_wifi_hint_explains_open_network_and_password_exposure(panel):
    panel.cb_type.setCurrentIndex(2)
    text = panel.type_hint.text().lower()
    assert "开放网络" in text or "open network" in text
    assert "密码" in text or "password" in text


def test_card_hint_names_vcard_boundaries(panel):
    panel.cb_type.setCurrentIndex(3)
    text = panel.type_hint.text()
    assert "BEGIN:VCARD" in text
    assert "END:VCARD" in text


def test_generation_and_dirty_states_update_preview_guidance(
        panel, monkeypatch):
    monkeypatch.setattr(
        "core.qr_maker.make_fancy_qr",
        lambda *_args, **_kwargs: Image.new("RGB", (200, 200), "white"))
    monkeypatch.setattr(panel, "_show_preview", lambda _image: None)
    panel.cb_type.setCurrentIndex(0)
    panel.txt_content.setPlainText("Hello")

    panel._generate()
    assert "试扫" in panel.preview_hint.text() or "real phone" in panel.preview_hint.text()
    panel.txt_content.setPlainText("Changed")
    assert "失效" in panel.preview_hint.text() or "out of date" in panel.preview_hint.text()
    assert not panel.btn_save.isEnabled()


def test_wifi_fields_reflow_without_horizontal_overflow(panel, app):
    panel.cb_type.setCurrentIndex(2)
    panel.resize(640, 820)
    panel.show()
    app.processEvents()

    assert panel.workspace_lay.direction() == QBoxLayout.TopToBottom
    assert panel.wifi_grid._columns == 1
    assert panel.horizontalScrollBar().maximum() == 0
