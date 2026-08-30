"""文件安全工具：认证加密、粉碎、签名验签与自签名证书。"""

import os
import re

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QWidget
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, LineEdit, MessageBox,
                            PushButton, SegmentedWidget, ToolButton)

from gui_qt import task_manager as tm
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

ALL_EXTS = None
ENCRYPTED_EXTS = (".fmsec", ".fmgcm", ".fmpub")
ALGO_VALUES = [
    ("fernet", tr("AES-CBC (Fernet, 兼容旧版)", "AES-CBC (Fernet, legacy)")),
    ("gcm", tr("AES-GCM (推荐)", "AES-GCM (recommended)")),
    ("rsa", tr("RSA 混合加密 (需密钥)", "RSA hybrid (needs a key)")),
    ("ecc", tr("ECC 混合加密 (需密钥)", "ECC hybrid (needs a key)")),
]
MODE_GUIDANCE = {
    "encrypt": tr(
        "生成新的加密副本，源文件保持不变。推荐使用 AES-GCM；输出重名时会自动改名。",
        "Creates encrypted copies and keeps source files unchanged. AES-GCM is recommended; conflicting outputs are renamed."),
    "decrypt": tr(
        "从 .fmsec、.fmgcm 或 .fmpub 文件恢复新副本，加密源文件保持不变。",
        "Restores new copies from .fmsec, .fmgcm, or .fmpub files and keeps encrypted sources unchanged."),
    "shred": tr(
        "永久覆写并删除源文件，无法撤销。SSD、APFS 快照或云同步副本仍可能保留数据。",
        "Permanently overwrites and deletes source files. This cannot be undone; SSDs, APFS snapshots, or cloud copies may retain data."),
    "sign": tr(
        "使用 PEM 私钥为每个文件生成 .sig 签名，源文件保持不变。",
        "Creates a .sig signature for each file with a PEM private key and keeps source files unchanged."),
    "verify": tr(
        "使用 PEM 公钥检查同名 .sig 文件，不修改源文件。签名会在源目录或所选目录中查找。",
        "Checks matching .sig files with a PEM public key without changing sources. Signatures are searched beside sources or in the selected folder."),
    "cert": tr(
        "生成 .crt 证书和密码加密的 .key 私钥，无需添加文件。选择“与源文件同目录”时保存到下载目录。",
        "Creates a .crt certificate and password-encrypted .key without input files. Same folder saves them to Downloads."),
}
MODE_ACTION_LABELS = {
    "encrypt": tr("加密文件", "Encrypt files"),
    "decrypt": tr("解密文件", "Decrypt files"),
    "shred": tr("永久粉碎", "Shred permanently"),
    "sign": tr("签名文件", "Sign files"),
    "verify": tr("验证签名", "Verify signatures"),
    "cert": tr("生成证书", "Generate certificate"),
}


class FileSecurityPanelPage(BaseQtPanel, TaskPanelMixin):
    """高风险操作先校验、确认，再交由不可自动修复的任务执行。"""

    panel_key = "file_security"
    need_ffmpeg = False

    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("文件安全工具", "File Security"),
            tr("认证加密、可靠解密、签名验签与证书生成",
               "Authenticated encryption, signatures and certificates"),
            FluentIcon.FINGERPRINT))

        mode_section = FormSection(tr("安全操作", "Security operation"),
                                   FluentIcon.CERTIFICATE)
        self.sg_mode = SegmentedWidget()
        for key, zh, en in (
                ("encrypt", "加密", "Encrypt"),
                ("decrypt", "解密", "Decrypt"),
                ("shred", "粉碎删除", "Shred"),
                ("sign", "数字签名", "Sign"),
                ("verify", "验签", "Verify"),
                ("cert", "生成证书", "Certificate")):
            self.sg_mode.addItem(key, tr(zh, en))
        self.sg_mode.setCurrentItem("encrypt")
        self.sg_mode.currentItemChanged.connect(self._mode_changed)
        self.sg_mode.setAccessibleName(tr("安全操作", "Security operation"))
        mode_section.add_widget(self.sg_mode)
        self.mode_hint = CaptionLabel(MODE_GUIDANCE["encrypt"])
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setProperty("sec", True)
        self.mode_hint.setAccessibleName(
            tr("当前操作说明", "Current operation guidance"))
        mode_section.add_widget(self.mode_hint)
        lay.addWidget(mode_section)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=ALL_EXTS)
        lay.addWidget(self.file_card)

        self.settings_section = FormSection(tr("安全参数", "Security settings"),
                                            FluentIcon.SETTING)
        self.settings_grid = FormGrid(columns=2)
        self.cb_algo = ComboBox()
        for _key, label in ALGO_VALUES:
            self.cb_algo.addItem(label)
        # 新用户优先使用认证加密；已有偏好仍会在页面构建后按稳定键恢复。
        self.cb_algo.setCurrentIndex(1)
        self.cb_algo.currentIndexChanged.connect(self._algo_changed)
        self.w_algo = self._wrap(self.cb_algo)
        self.settings_grid.add_field(tr("算法", "Algorithm"), self.w_algo)

        self.ed_key = LineEdit()
        self.ed_key.setReadOnly(True)
        self.ed_key.setPlaceholderText(tr("选择 PEM 公钥或私钥…", "Choose a PEM key…"))
        self.btn_key = PushButton(FluentIcon.DOCUMENT, tr("选择", "Choose"))
        self.btn_key.clicked.connect(self._pick_key)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.ed_key, 1)
        row.addWidget(self.btn_key)
        self.w_key = QWidget()
        self.w_key.setLayout(row)
        self.settings_grid.add_field(tr("密钥文件", "Key file"), self.w_key)

        self.ed_pw = LineEdit()
        self.ed_pw.setEchoMode(LineEdit.EchoMode.Password)
        self.ed_pw.setPlaceholderText(tr("至少 8 个字符", "At least 8 characters"))
        self.btn_eye = ToolButton(FluentIcon.VIEW)
        self.btn_eye.setAccessibleName(tr("显示或隐藏密码", "Show or hide password"))
        self.btn_eye.setToolTip(self.btn_eye.accessibleName())
        self.btn_eye.clicked.connect(self._toggle_password)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.ed_pw, 1)
        row.addWidget(self.btn_eye)
        self.w_pw = QWidget()
        self.w_pw.setLayout(row)
        self.settings_grid.add_field(
            tr("密码", "Password"), self.w_pw,
            hint=tr("仅在当前任务内存中使用，不写入偏好或任务快照",
                    "Used in memory only; never saved to preferences or snapshots"))

        self.ed_pw2 = LineEdit()
        self.ed_pw2.setEchoMode(LineEdit.EchoMode.Password)
        self.ed_pw2.setPlaceholderText(tr("再次输入密码", "Enter password again"))
        self.settings_grid.add_field(tr("确认密码", "Confirm password"), self.ed_pw2)
        self.ed_cn = LineEdit()
        self.ed_cn.setPlaceholderText(tr("如 myapp.local", "e.g. myapp.local"))
        self.settings_grid.add_field(tr("证书名称 (CN)", "Common name"), self.ed_cn)
        self.ed_days = LineEdit()
        self.ed_days.setText("365")
        self.ed_days.setPlaceholderText("1-3650")
        self.settings_grid.add_field(tr("有效期（天）", "Validity (days)"), self.ed_days)
        self.settings_section.add_form(self.settings_grid)
        lay.addWidget(self.settings_section)

        self.output_section = FormSection(
            tr("输出位置", "Output location"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        # 本页英文模式名较长，局部增加宽度，避免截断共享组件的标准文案。
        self.out_row.mode_combo.setFixedWidth(192)
        self.out_row.bind_file_list(self.file_card)
        self.output_section.add_widget(self.out_row)
        lay.addWidget(self.output_section)
        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)
        self._wire_tasks()
        self._last_mode = self._mode()
        self._mode_changed(self._last_mode)

    @staticmethod
    def _wrap(control):
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(control)
        return wrapper

    def _mode(self):
        return self.sg_mode.currentRouteKey()

    def _algo_key(self):
        return ALGO_VALUES[max(0, self.cb_algo.currentIndex())][0]

    def _mode_changed(self, _key=None):
        mode = self._mode()
        if hasattr(self, "_last_mode") and mode != self._last_mode:
            self.file_card.clear_files()
            self.ed_pw.clear()
            self.ed_pw2.clear()
            self.ed_key.clear()
        self._last_mode = mode
        self.file_card.setVisible(mode != "cert")
        self.out_row.setVisible(mode != "shred")
        self.output_section.setVisible(mode != "shred")
        self.settings_section.setVisible(mode != "shred")
        self.mode_hint.setText(MODE_GUIDANCE[mode])
        self.action_bar.btn_go.setText(MODE_ACTION_LABELS[mode])
        self.action_bar.btn_go.setAccessibleName(MODE_ACTION_LABELS[mode])
        show_algo = mode in ("encrypt", "decrypt")
        asymmetric = mode in ("encrypt", "decrypt") and self._algo_key() in ("rsa", "ecc")
        show_key = mode in ("sign", "verify") or asymmetric
        password_mode = ((mode in ("encrypt", "decrypt") and not asymmetric)
                         or (mode == "decrypt" and asymmetric)
                         or mode in ("sign", "cert"))
        show_confirm = mode in ("encrypt", "cert") and password_mode
        if mode in ("sign",) or (mode == "decrypt" and asymmetric):
            self.ed_pw.setPlaceholderText(
                tr("私钥未加密时可留空", "Leave blank for an unencrypted private key"))
        elif mode == "decrypt":
            self.ed_pw.setPlaceholderText(tr("输入解密密码", "Enter decryption password"))
        else:
            self.ed_pw.setPlaceholderText(tr("至少 8 个字符", "At least 8 characters"))
        if mode == "sign" or (mode == "decrypt" and asymmetric):
            self.ed_key.setPlaceholderText(
                tr("选择 PEM 私钥…", "Choose a PEM private key…"))
        elif mode == "verify" or (mode == "encrypt" and asymmetric):
            self.ed_key.setPlaceholderText(
                tr("选择 PEM 公钥…", "Choose a PEM public key…"))
        else:
            self.ed_key.setPlaceholderText(
                tr("选择 PEM 公钥或私钥…", "Choose a PEM key…"))
        self._set_field_visible(0, show_algo)
        self._set_field_visible(1, show_key)
        self._set_field_visible(2, password_mode)
        self._set_field_visible(3, show_confirm)
        self._set_field_visible(4, mode == "cert")
        self._set_field_visible(5, mode == "cert")
        self._sync_start_enabled()

    def _set_field_visible(self, index, visible):
        """同时隐藏字段标签和控件，避免只剩无意义的空标签。"""
        self.settings_grid.set_field_visible(index, visible)

    def _algo_changed(self, _index=None):
        # 算法变化只切换相关参数，不丢弃用户已添加的文件。
        self.ed_pw.clear()
        self.ed_pw2.clear()
        self.ed_key.clear()
        self._mode_changed(self._mode())

    def _pick_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择密钥文件", "Choose key file"), "",
            tr("PEM 密钥 (*.pem *.key);;所有文件 (*)",
               "PEM keys (*.pem *.key);;All files (*)"))
        if path:
            self.ed_key.setText(path)

    def _toggle_password(self):
        mode = (LineEdit.EchoMode.Normal
                if self.ed_pw.echoMode() == LineEdit.EchoMode.Password
                else LineEdit.EchoMode.Password)
        self.ed_pw.setEchoMode(mode)
        self.ed_pw2.setEchoMode(mode)

    def collect_params(self):
        return {"mode": self._mode(), "algo": self._algo_key(),
                "password": self.ed_pw.text(),
                "key_path": self.ed_key.text().strip(),
                "cn": self.ed_cn.text().strip(), "days": self.ed_days.text().strip()}

    def _validate_inputs(self):
        p = self.collect_params()
        mode, algo = p["mode"], p["algo"]
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path() and mode != "shred":
            return tr("请先选择自定义输出目录", "Choose an output folder first")
        if mode == "cert":
            if not p["cn"] or len(p["cn"]) > 253 or any(ord(c) < 32 for c in p["cn"]):
                return tr("请输入有效的证书名称（最长 253 字符）",
                          "Enter a valid common name (253 characters max)")
            try:
                days = int(p["days"])
            except ValueError:
                days = 0
            if not 1 <= days <= 3650:
                return tr("有效期必须是 1-3650 的整数",
                          "Validity must be an integer from 1 to 3650")
        if mode in ("encrypt", "cert") and p["password"] != self.ed_pw2.text():
            return tr("两次输入的密码不一致", "Passwords do not match")
        if ((mode in ("encrypt", "decrypt") and algo not in ("rsa", "ecc")) or mode == "cert"):
            minimum = 8 if mode in ("encrypt", "cert") else 1
            if len(p["password"]) < minimum:
                return (tr("密码至少需要 8 个字符", "Password must be at least 8 characters")
                        if minimum == 8 else tr("请输入解密密码", "Enter the decryption password"))
        if mode in ("sign", "verify") or (mode in ("encrypt", "decrypt") and algo in ("rsa", "ecc")):
            if not os.path.isfile(p["key_path"]):
                return tr("请选择有效的 PEM 密钥文件", "Choose a valid PEM key file")
        if mode == "decrypt" and any(
                not path.lower().endswith(ENCRYPTED_EXTS) for path in self.file_card.files()):
            return tr("解密仅支持 .fmsec、.fmgcm 或 .fmpub 文件",
                      "Decrypt supports .fmsec, .fmgcm or .fmpub files only")
        return ""

    def _safe_output(self, source, desired_name):
        stem, ext = os.path.splitext(desired_name)
        return tm.make_output_path(source, self.out_row.resolve_dir(source), ext,
                                   name=stem, conflict="auto_rename")

    def _runner(self, task, prog):
        p = task.params
        mode, algo = p["mode"], p["algo"]
        if mode == "shred":
            from core.file_security import shred_file
            return shred_file(task.file_path, progress_cb=prog)
        if mode == "sign":
            from core.crypto_advanced import sign_file
            with open(p["key_path"], "rb") as stream:
                return sign_file(
                    task.file_path, stream.read(), task.output_path, prog,
                    private_key_password=p["password"] or None) is not None
        if mode == "verify":
            from core.crypto_advanced import verify_signature
            with open(p["key_path"], "rb") as stream:
                ok, message = verify_signature(task.file_path, stream.read(), p["signature_path"], prog)
            if not ok and prog:
                prog(-1, message)
            return ok
        if mode == "cert":
            from core.crypto_advanced import generate_self_signed_cert
            cert, _key = generate_self_signed_cert(
                p["cn"], task.output_path, private_key_path=p["private_key_path"],
                days=int(p["days"]), progress_cb=prog,
                private_key_password=p["password"])
            return cert is not None
        if algo in ("rsa", "ecc"):
            from core.crypto_advanced import decrypt_asymmetric, encrypt_asymmetric
            with open(p["key_path"], "rb") as stream:
                pem = stream.read()
            fn = encrypt_asymmetric if mode == "encrypt" else decrypt_asymmetric
            if mode == "encrypt":
                return fn(task.file_path, task.output_path, pem, progress_cb=prog)
            return fn(task.file_path, task.output_path, pem, progress_cb=prog,
                      private_key_password=p["password"] or None)
        if algo == "gcm":
            from core.crypto_advanced import decrypt_file_gcm, encrypt_file_gcm
            fn = encrypt_file_gcm if mode == "encrypt" else decrypt_file_gcm
        else:
            from core.file_security import decrypt_file, encrypt_file
            fn = encrypt_file if mode == "encrypt" else decrypt_file
        return fn(task.file_path, task.output_path, p["password"], progress_cb=prog)

    def _make_task(self, source):
        p = self.collect_params()
        mode, algo = p["mode"], p["algo"]
        if mode == "shred":
            output, target = source, tr("粉碎", "Shred")
        elif mode == "sign":
            output = self._safe_output(source, os.path.basename(source) + ".sig")
            target = tr("签名", "Sign")
        elif mode == "verify":
            candidates = [source + ".sig",
                          os.path.join(self.out_row.resolve_dir(source), os.path.basename(source) + ".sig")]
            p["signature_path"] = next((x for x in candidates if os.path.isfile(x)), "")
            if not p["signature_path"]:
                toast.show_warning(self, tr("未找到同名 .sig 签名文件",
                                            "Matching .sig file not found"))
                return None
            output, target = source, tr("验签", "Verify")
        elif mode == "encrypt":
            ext = {"fernet": ".fmsec", "gcm": ".fmgcm",
                   "rsa": ".fmpub", "ecc": ".fmpub"}[algo]
            output = self._safe_output(source, os.path.basename(source) + ext)
            target = tr("加密", "Encrypt")
        else:
            name = os.path.basename(source)
            output = self._safe_output(source, name[:-len(os.path.splitext(name)[1])])
            target = tr("解密", "Decrypt")
        return self._task_kwargs(source, output, p, target)

    def _task_kwargs(self, source, output, params, target):
        return {"name": f"{target} - {os.path.basename(source)}",
                "task_type": "file_security", "file_path": source,
                "output_path": output, "params": params, "runner": self._runner,
                "history_type": tr("文件安全", "File Security"),
                "history_target": target, "need_ffmpeg": False,
                "max_retries": 0, "allow_auto_recover": False,
                "sensitive_param_keys": ("password",)}

    def _certificate_task(self):
        p = self.collect_params()
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p["cn"]).strip("._") or "certificate"
        # 证书模式没有源文件，“同目录”语义回退到用户下载目录。
        out_dir = self.out_row.resolve_dir("") or os.path.expanduser("~/Downloads")
        candidate = safe
        counter = 0
        while True:
            cert_path = os.path.join(out_dir, candidate + ".crt")
            key_path = os.path.join(out_dir, candidate + ".key")
            if not os.path.exists(cert_path) and not os.path.exists(key_path):
                break
            counter += 1
            candidate = f"{safe}_{counter}"
        p["private_key_path"] = key_path
        return self._task_kwargs("", cert_path, p, tr("证书", "Certificate"))

    def _confirm_shred(self, count):
        box = MessageBox(
            tr("确认永久粉碎？", "Permanently shred files?"),
            tr("将覆写并删除 {} 个源文件，无法撤销。SSD、APFS 快照和云同步副本可能仍保留数据。",
               "This overwrites and deletes {} source files and cannot be undone. SSDs, APFS snapshots, or cloud copies may retain data.").format(count),
            self)
        box.yesButton.setText(tr("永久粉碎", "Shred permanently"))
        box.cancelButton.setText(tr("取消", "Cancel"))
        accepted = [False]
        box.yesButton.clicked.connect(lambda: accepted.__setitem__(0, True))
        box.exec()
        return accepted[0]

    def _start(self):
        error = self._validate_inputs()
        if error:
            toast.show_warning(self, error)
            return False
        if self._mode() == "shred" and not self.file_card.files():
            toast.show_warning(self, self._empty_hint())
            return False
        if self._mode() == "shred" and not self._confirm_shred(len(self.file_card.files())):
            return False
        if self._mode() == "cert":
            self.save_prefs()
            kwargs = self._certificate_task()
            task_id = self.services.task_manager.add_task(**kwargs)
            self._task_rows[task_id] = (kwargs["output_path"], -1)
            self._batch_progress[task_id] = 0
            self.action_bar.set_running(True)
            self.action_bar.set_status(tr("证书生成任务已提交", "Certificate task submitted"))
            submitted = True
        else:
            submitted = self._submit_files()
        if submitted:
            self.ed_pw.clear()
            self.ed_pw2.clear()
            self._sync_start_enabled()
        return submitted

    def _empty_hint(self):
        return tr("请先添加要处理的文件", "Add files first")

    def _sync_start_enabled(self):
        if not hasattr(self, "action_bar"):
            return
        enabled = not self._task_rows and (self._mode() == "cert" or bool(self.file_card.files()))
        self.action_bar.btn_go.setEnabled(enabled)
        self.action_bar.btn_go.setToolTip("" if enabled else self._empty_hint())
        for widget in (self.sg_mode, self.file_card, self.settings_section, self.out_row):
            widget.setEnabled(not self._task_rows)

    def collect_prefs(self):
        return {"mode": self._mode(), "algo": self._algo_key()}

    def apply_prefs(self, prefs):
        if not prefs:
            return
        mode = prefs.get("mode")
        if mode in ("encrypt", "decrypt", "shred", "sign", "verify", "cert"):
            self.sg_mode.setCurrentItem(mode)
        algo = prefs.get("algo")
        keys = [key for key, _label in ALGO_VALUES]
        if algo in keys:
            self.cb_algo.setCurrentIndex(keys.index(algo))
        elif isinstance(algo, int) and 0 <= algo < len(ALGO_VALUES):
            self.cb_algo.setCurrentIndex(algo)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "settings_grid"):
            self.settings_grid.set_columns(1 if self.width() < 760 else 2)
