"""文件安全菜单的认证、原子写入、隐私与页面回归测试。"""

import json
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
    from gui_qt.panels.file_security_panel import FileSecurityPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = FileSecurityPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_fernet_roundtrip_and_wrong_password_preserves_target(tmp_path,
                                                               monkeypatch):
    from core import file_security

    monkeypatch.setattr(file_security, "_BLOCK", 5)
    source = tmp_path / "source.bin"
    encrypted = tmp_path / "source.bin.fmsec"
    restored = tmp_path / "restored.bin"
    source.write_bytes(b"authenticated-content" * 4)
    restored.write_bytes(b"keep-existing")

    assert file_security.encrypt_file(str(source), str(encrypted), "correct horse")
    assert not file_security.decrypt_file(
        str(encrypted), str(restored), "wrong password")
    assert restored.read_bytes() == b"keep-existing"
    assert file_security.decrypt_file(
        str(encrypted), str(restored), "correct horse")
    assert restored.read_bytes() == source.read_bytes()
    assert not list(tmp_path.glob(".fm_security_*"))


def test_gcm_multiblock_unique_nonce_and_truncation_is_atomic(tmp_path,
                                                               monkeypatch):
    from core import crypto_advanced

    monkeypatch.setattr(crypto_advanced, "_BLOCK", 4)
    assert crypto_advanced._chunk_nonce(b"123456789012", 0) != \
        crypto_advanced._chunk_nonce(b"123456789012", 1)
    source = tmp_path / "source.bin"
    encrypted = tmp_path / "source.fmgcm"
    restored = tmp_path / "restored.bin"
    source.write_bytes(b"0123456789abcdef")
    assert crypto_advanced.encrypt_file_gcm(
        str(source), str(encrypted), "strong-password")
    assert crypto_advanced.decrypt_file_gcm(
        str(encrypted), str(restored), "strong-password")
    assert restored.read_bytes() == source.read_bytes()

    damaged = tmp_path / "damaged.fmgcm"
    damaged.write_bytes(encrypted.read_bytes()[:-8])
    restored.write_bytes(b"existing")
    assert not crypto_advanced.decrypt_file_gcm(
        str(damaged), str(restored), "strong-password")
    assert restored.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".fm_crypto_*"))


@pytest.mark.parametrize("algorithm", ["fernet", "gcm"])
def test_empty_file_still_authenticates_password(tmp_path, algorithm):
    """空文件也必须写认证块，否则任意密码都会被误判为解密成功。"""
    source = tmp_path / f"empty-{algorithm}"
    encrypted = tmp_path / f"empty-{algorithm}.enc"
    restored = tmp_path / f"empty-{algorithm}.out"
    source.write_bytes(b"")
    if algorithm == "fernet":
        from core.file_security import decrypt_file, encrypt_file
    else:
        from core.crypto_advanced import decrypt_file_gcm as decrypt_file
        from core.crypto_advanced import encrypt_file_gcm as encrypt_file
    assert encrypt_file(str(source), str(encrypted), "right-password")
    assert not decrypt_file(str(encrypted), str(restored), "wrong-password")
    assert not restored.exists()
    assert decrypt_file(str(encrypted), str(restored), "right-password")
    assert restored.read_bytes() == b""


def test_asymmetric_multiblock_roundtrip(tmp_path, monkeypatch):
    from core import crypto_advanced

    monkeypatch.setattr(crypto_advanced, "_BLOCK", 3)
    private, public = crypto_advanced.generate_keypair("rsa")
    source = tmp_path / "rsa.bin"
    encrypted = tmp_path / "rsa.fmpub"
    restored = tmp_path / "rsa-restored.bin"
    source.write_bytes(b"many-blocks-for-rsa")
    assert crypto_advanced.encrypt_asymmetric(
        str(source), str(encrypted), public)
    assert crypto_advanced.decrypt_asymmetric(
        str(encrypted), str(restored), private)
    assert restored.read_bytes() == source.read_bytes()


def test_certificate_private_key_is_encrypted_and_restricted(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from core.crypto_advanced import generate_self_signed_cert

    cert_path = tmp_path / "service.crt"
    key_path = tmp_path / "service.key"
    cert, key = generate_self_signed_cert(
        "service.local", str(cert_path), private_key_path=str(key_path),
        days=30, private_key_password="certificate-secret")
    assert cert == str(cert_path) and key == str(key_path)
    with pytest.raises((TypeError, ValueError)):
        serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    loaded = serialization.load_pem_private_key(
        key_path.read_bytes(), password=b"certificate-secret")
    assert loaded is not None
    assert key_path.stat().st_mode & 0o077 == 0
    # 页面生成的加密私钥可直接用于本工具的签名流程。
    from core.crypto_advanced import sign_file, verify_signature
    source = tmp_path / "signed.txt"
    source.write_text("signed content", encoding="utf-8")
    public = loaded.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    signature = sign_file(
        str(source), key_path.read_bytes(),
        private_key_password="certificate-secret")
    assert signature and verify_signature(str(source), public, signature)[0]


def test_sensitive_task_params_never_enter_snapshot(tmp_path):
    from gui_qt.task_manager import Task, TaskManager, WAITING

    task = Task(1, "secure", "file_security", "a", "b",
                params={"password": "top-secret", "mode": "encrypt"},
                state=WAITING, sensitive_param_keys=("password",),
                allow_auto_recover=False)

    class FakeManager:
        _tasks = {1: task}
        _shutting_down = False
        sig_log = type("Signal", (), {"emit": lambda *_args: None})()

        @staticmethod
        def _snapshot_path():
            return str(tmp_path / "queue.json")

    TaskManager._save_snapshot_now(FakeManager())
    saved = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert "password" not in saved[0]["params"]
    assert saved[0]["allow_auto_recover"] is False


def test_panel_algorithm_change_keeps_files_and_outputs_auto_rename(panel,
                                                                    tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("secret", encoding="utf-8")
    panel.out_row.set_state(OutputDirRow.MODE_SAME)
    panel.file_card.add_files([str(source)])
    panel.cb_algo.setCurrentIndex(1)
    assert panel.file_card.files() == [str(source)]

    panel.ed_pw.setText("password-123")
    panel.ed_pw2.setText("password-123")
    existing = tmp_path / "report.txt.fmgcm"
    existing.write_bytes(b"existing")
    task = panel._make_task(str(source))
    assert task["output_path"].endswith("report.txt_1.fmgcm")
    assert task["allow_auto_recover"] is False
    assert task["sensitive_param_keys"] == ("password",)


def test_certificate_mode_needs_no_input_and_sanitizes_filename(panel,
                                                                 tmp_path):
    panel.sg_mode.setCurrentItem("cert")
    panel.out_row.set_state(OutputDirRow.MODE_CUSTOM, str(tmp_path))
    panel.ed_cn.setText("../../my service.local")
    panel.ed_days.setText("365")
    panel.ed_pw.setText("password-123")
    panel.ed_pw2.setText("password-123")
    assert panel._validate_inputs() == ""
    task = panel._certificate_task()
    assert os.path.dirname(task["output_path"]) == str(tmp_path)
    assert ".." not in os.path.basename(task["output_path"])
    assert task["params"]["private_key_path"].endswith(".key")
    # 孤立的同名私钥也属于冲突，不能被新证书任务复用或覆盖。
    open(task["params"]["private_key_path"], "wb").close()
    second = panel._certificate_task()
    assert second["output_path"] != task["output_path"]


def test_narrow_panel_uses_single_column(panel, app):
    panel.resize(650, 800)
    panel.show()
    app.processEvents()
    assert panel.settings_grid._columns == 1
    assert panel.horizontalScrollBar().maximum() == 0


def test_mode_guidance_explains_outputs_and_source_impact(panel):
    panel.sg_mode.setCurrentItem("encrypt")
    assert "源文件保持不变" in panel.mode_hint.text() or "source files unchanged" in panel.mode_hint.text()
    assert "AES-GCM" in panel.mode_hint.text()

    panel.sg_mode.setCurrentItem("verify")
    assert ".sig" in panel.mode_hint.text()
    assert "不修改源文件" in panel.mode_hint.text() or "without changing sources" in panel.mode_hint.text()


def test_shred_hides_irrelevant_sections_and_uses_explicit_action(panel):
    panel.sg_mode.setCurrentItem("shred")

    assert panel.settings_section.isHidden()
    assert panel.output_section.isHidden()
    assert "无法撤销" in panel.mode_hint.text() or "cannot be undone" in panel.mode_hint.text()
    assert panel.action_bar.btn_go.text() in ("永久粉碎", "Shred permanently")


def test_certificate_guidance_explains_download_fallback_and_encrypted_key(
        panel):
    panel.sg_mode.setCurrentItem("cert")

    assert panel.file_card.isHidden()
    assert not panel.output_section.isHidden()
    assert ".key" in panel.mode_hint.text()
    assert "下载目录" in panel.mode_hint.text() or "Downloads" in panel.mode_hint.text()
    assert panel.action_bar.btn_go.text() in ("生成证书", "Generate certificate")


# Imported late to keep Qt environment initialization above module imports.
from gui_qt.widgets import OutputDirRow  # noqa: E402
