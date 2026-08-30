"""二维码可靠性、输入规范、偏好和原子保存回归测试。"""

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
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def _first_dark_pixel(image):
    pixels = image.load()
    background = pixels[0, 0]
    return min(x for y in range(image.height) for x in range(image.width)
               if pixels[x, y] != background)


def test_border_is_real_quiet_zone_and_respects_setting():
    from core.qr_maker import make_fancy_qr

    border4 = make_fancy_qr("hello", size=400, border=4)
    border8 = make_fancy_qr("hello", size=400, border=8)

    assert _first_dark_pixel(border4) > 0
    assert _first_dark_pixel(border8) > _first_dark_pixel(border4)
    assert border4.getpixel((0, 0)) == border4.getpixel((399, 399))


def test_low_contrast_missing_logo_and_too_dense_content_are_rejected(tmp_path):
    from core.qr_maker import make_fancy_qr, RECOMMENDED_MODULE_PIXELS

    with pytest.raises(ValueError, match="对比度"):
        make_fancy_qr("hello", fg="#777777", bg="#888888")
    with pytest.raises(ValueError, match="Logo"):
        make_fancy_qr("hello", logo_path=str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="至少"):
        make_fancy_qr(
            "x" * 500, size=300,
            min_module_pixels=RECOMMENDED_MODULE_PIXELS)


def test_styled_gradient_logo_qr_remains_decodable(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from core.qr_maker import (GRAD_DIAGONAL, STYLE_DIAMOND,
                               make_fancy_qr)

    logo = tmp_path / "logo.png"
    Image.new("RGB", (80, 80), "#D84B4B").save(logo)
    content = "https://example.com/path?value=42"
    image = make_fancy_qr(
        content, size=500, fg="#2457A6", style=STYLE_DIAMOND,
        gradient=GRAD_DIAGONAL, logo_path=str(logo))
    decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(
        np.array(image)[:, :, ::-1])

    assert decoded == content


def test_wifi_escaping_and_url_normalization():
    from gui_qt.panels.qrcode_panel import _escape_wifi, _normalize_url

    assert _escape_wifi(r"Office;5G:A\B") == "Office\\;5G\\:A\\\\B"
    assert _normalize_url("example.com/path") == "https://example.com/path"
    assert _normalize_url("https://") == ""
    assert _normalize_url("ftp://example.com") == ""


def test_switching_types_preserves_each_text_draft(panel):
    panel.cb_type.setCurrentIndex(0)
    panel.txt_content.setPlainText("personal note")
    panel.cb_type.setCurrentIndex(1)
    panel.txt_content.setPlainText("example.com")
    panel.cb_type.setCurrentIndex(0)

    assert panel.txt_content.toPlainText() == "personal note"
    panel.cb_type.setCurrentIndex(1)
    assert panel.txt_content.toPlainText() == "example.com"


def test_panel_builds_normalized_url_and_escaped_wifi_payload(
        panel, monkeypatch):
    captured = []

    def fake_make(content, **_kwargs):
        captured.append(content)
        return Image.new("RGB", (200, 200), "white")

    monkeypatch.setattr("core.qr_maker.make_fancy_qr", fake_make)
    monkeypatch.setattr(panel, "_show_preview", lambda _image: None)

    panel.cb_type.setCurrentIndex(1)
    panel.txt_content.setPlainText("example.com/docs")
    panel._generate()
    panel.cb_type.setCurrentIndex(2)
    panel.ed_ssid.setText("Office;5G")
    panel.ed_pass.setText("a:b\\c")
    panel._generate()

    assert captured[0] == "https://example.com/docs"
    assert captured[1] == "WIFI:T:WPA;S:Office\\;5G;P:a\\:b\\\\c;;"


def test_editing_after_generation_invalidates_old_saved_result(panel):
    panel._qr_img = Image.new("RGB", (200, 200), "white")
    panel.btn_save.setEnabled(True)

    panel.txt_content.setPlainText("changed")

    assert panel._qr_img is None
    assert not panel.btn_save.isEnabled()
    assert "重新生成" in panel.lb_status.text() or "generate again" in panel.lb_status.text()


def test_preferences_use_stable_keys_and_restore_legacy_values(panel):
    from gui_qt.panels.qrcode_panel import TYPE_VALUES

    panel.apply_prefs({
        "qr_type": TYPE_VALUES[3], "qr_size": "500", "qr_border": "1",
        "qr_fg": "#112233", "qr_bg": "#FFFFFF",
        "qr_style": 2, "qr_grad": 1,
    })
    prefs = panel.collect_prefs()

    assert prefs["qr_type"] == "card"
    assert prefs["qr_border"] == "4"
    assert prefs["qr_style"] == "dot"
    assert prefs["qr_grad"] == "vertical"


def test_save_adds_extension_and_preserves_existing_file_on_failure(
        panel, monkeypatch, tmp_path):
    target_without_suffix = tmp_path / "generated"
    panel._qr_img = Image.new("RGB", (200, 200), "white")
    monkeypatch.setattr(
        "gui_qt.panels.qrcode_panel.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(target_without_suffix), "PNG images (*.png)"))
    monkeypatch.setattr(
        "gui_qt.panels.qrcode_panel.toast.show_success",
        lambda *_args, **_kwargs: None)
    panel._save()
    assert (tmp_path / "generated.png").is_file()

    existing = tmp_path / "existing.png"
    existing.write_bytes(b"previous")

    class BrokenImage:
        def save(self, _path):
            raise OSError("disk full")

    panel._qr_img = BrokenImage()
    monkeypatch.setattr(
        "gui_qt.panels.qrcode_panel.QFileDialog.getSaveFileName",
        lambda *_args, **_kwargs: (str(existing), "PNG images (*.png)"))
    monkeypatch.setattr(
        "gui_qt.panels.qrcode_panel.toast.show_error",
        lambda *_args, **_kwargs: None)
    panel._save()

    assert existing.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".fm_qrcode_*.png"))


def test_workspace_reflows_on_narrow_window(panel, app):
    panel.resize(720, 760)
    panel.show()
    app.processEvents()
    assert panel.workspace_lay.direction() == QBoxLayout.TopToBottom
    assert panel.horizontalScrollBar().maximum() == 0
    panel.resize(1100, 760)
    app.processEvents()
    assert panel.workspace_lay.direction() == QBoxLayout.LeftToRight
