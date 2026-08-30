"""qrcode_panel — 二维码生成面板（阶段2 迁移自 gui/panels/qrcode_panel.py + main.py 生成逻辑）。

将文本/网址/WiFi/名片内容生成二维码图片（qrcode + Pillow），
支持自定义尺寸、边距与前后颜色，实时预览并可保存为图片。
生成为毫秒级纯内存操作，直接在主线程同步执行。
"""
import os
import tempfile
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QBoxLayout, QFileDialog, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (FluentIcon, CaptionLabel, ComboBox, LineEdit,
                            PasswordLineEdit, PrimaryPushButton, PushButton,
                            TextEdit)

from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import ActionBar, ActionStatusState

# 预置值（与 tkinter 版 qrcode_panel 一致）
TYPE_KEYS = ("text", "url", "wifi", "card")
TYPE_VALUES = [tr("文本", "Text"), tr("网址", "URL"), "WiFi", tr("名片", "Card")]
SIZE_VALUES = ["200", "300", "400", "500", "600"]
BORDER_VALUES = ["4", "6", "8"]
STYLE_KEYS = ("square", "rounded", "dot", "diamond")
GRADIENT_KEYS = ("none", "vertical", "diagonal")
DEFAULT_FG = "#000000"
DEFAULT_BG = "#FFFFFF"

VCARD_TPL = tr("BEGIN:VCARD\nVERSION:3.0\nFN:姓名\nTEL:13800138000\nEMAIL:email@example.com\nEND:VCARD", "BEGIN:VCARD\nVERSION:3.0\nFN:Name\nTEL:13800138000\nEMAIL:email@example.com\nEND:VCARD")


def _escape_wifi(value):
    """按 WiFi QR 规范转义反斜杠及字段分隔符。"""
    escaped = str(value).replace("\\", "\\\\")
    for char in (";", ",", ":"):
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def _normalize_url(value):
    value = str(value).strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value


class QrcodePanelPage(BaseQtPanel):
    """二维码生成页。"""

    panel_key = "qrcode"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("二维码生成器", "QR Code Generator"),
            tr("将文本、链接、WiFi 或联系方式生成清晰、可扫描的二维码",
               "Create clear, scannable QR codes for text, links, WiFi, or contacts"),
            FluentIcon.QRCODE)
        lay.addWidget(self.header)

        from gui_qt.components.form_widgets import FormSection, FormGrid

        # 内容设置
        sec = FormSection(tr("内容设置", "Content"), FluentIcon.EDIT)
        grid = FormGrid(columns=1)

        self._drafts = {
            "text": "Hello World", "url": "https://", "card": VCARD_TPL,
        }
        self._active_type_key = "text"
        self.cb_type = grid.add_field(
            tr("内容类型", "Content type"), self._combo(TYPE_VALUES, tr("文本", "Text")),
            hint=tr("选择要生成的二维码内容类型", "Choose QR content type"))
        self.cb_type.currentTextChanged.connect(self._on_type_changed)

        self.txt_content = TextEdit()
        self.txt_content.setPlainText(self._drafts["text"])
        self.txt_content.setFixedHeight(80)
        from gui_qt.components import design_system as _ds
        _ds.apply_text_edit_style(self.txt_content)
        grid.add_field(tr("内容", "Content"), self.txt_content,
                        hint=tr("输入二维码内容", "Enter QR content"))

        # WiFi 区（默认隐藏）
        self.wifi_grid = FormGrid(columns=2)
        self.ed_ssid = LineEdit()
        self.ed_ssid.setPlaceholderText(tr("输入 WiFi 名称", "Enter WiFi name"))
        self.ed_pass = PasswordLineEdit()
        self.ed_pass.setPlaceholderText(tr("输入 WiFi 密码", "Enter WiFi password"))
        self.wifi_grid.add_field(tr("WiFi 名称", "WiFi name"), self.ed_ssid)
        self.wifi_grid.add_field(tr("密码", "Password"), self.ed_pass)
        wifi_holder = QWidget()
        wifi_holder.setLayout(self.wifi_grid)
        self.wifi_holder = wifi_holder
        self._set_wifi_visible(False)
        sec.add_form(grid)
        sec.add_widget(wifi_holder)
        self.type_hint = CaptionLabel()
        self.type_hint.setWordWrap(True)
        sec.add_widget(self.type_hint)
        self._sync_type_hint()

        # 外观设置
        from gui_qt.components.form_widgets import CollapsibleSection
        sec_style = FormSection(tr("外观设置", "Appearance"), FluentIcon.PALETTE)
        self.style_grid = FormGrid(columns=3)
        self.cb_size = self.style_grid.add_field(
            tr("尺寸", "Size"), self._combo(SIZE_VALUES, "400"),
            hint=tr("二维码像素尺寸", "QR pixel size"))
        self.cb_style = self.style_grid.add_field(
            tr("模块样式", "Module style"),
            self._combo([tr("经典方块", "Square"),
                         tr("圆角方块", "Rounded"),
                         tr("圆点", "Dots"),
                         tr("菱形", "Diamond")], tr("经典方块", "Square")),
            hint=tr("二维码图案样式", "QR module style"))
        self.cb_grad = self.style_grid.add_field(
            tr("渐变", "Gradient"),
            self._combo([tr("无", "None"), tr("垂直", "Vertical"),
                         tr("对角", "Diagonal")], tr("无", "None")),
            hint=tr("前景色渐变（搭配主题色更精致）", "Foreground gradient"))
        sec_style.add_form(self.style_grid)

        # Logo 行：选择 Logo 图片（可选，圆形遮罩+白边）
        logo_row = QHBoxLayout()
        logo_row.setSpacing(8)
        self.btn_logo = PushButton(FluentIcon.PHOTO, tr("选择 Logo…", "Pick Logo…"))
        self.btn_logo.clicked.connect(self._pick_logo)
        self.btn_logo_clear = PushButton(FluentIcon.CANCEL, tr("清除", "Clear"))
        self.btn_logo_clear.clicked.connect(self._clear_logo)
        self.btn_logo_clear.setEnabled(False)
        logo_row.addWidget(self.btn_logo)
        logo_row.addWidget(self.btn_logo_clear)
        self.lb_logo = CaptionLabel(tr("未选择（可选）", "None (optional)"))
        logo_row.addWidget(self.lb_logo)
        logo_row.addStretch(1)
        logo_holder = QWidget()
        logo_holder.setLayout(logo_row)
        sec_style.add_widget(logo_holder)

        adv = CollapsibleSection(
            tr("高级设置", "Advanced"),
            hint=tr("边距 / 前景色 / 背景色（通常无需调整）",
                    "Margin / foreground / background (usually leave as-is)"))
        adv_grid = FormGrid(columns=4)
        self.cb_border = adv_grid.add_field(
            tr("边距", "Margin"), self._combo(BORDER_VALUES, "4"),
            hint=tr("二维码空白边距", "QR margin"))
        self.ed_fg = adv_grid.add_field(
            tr("前景色", "Foreground"), self._make_color_edit(DEFAULT_FG),
            hint=tr("二维码图案颜色", "QR foreground color"))
        self.ed_bg = adv_grid.add_field(
            tr("背景色", "Background"), self._make_color_edit(DEFAULT_BG),
            hint=tr("二维码背景颜色", "QR background color"))
        adv.add_layout(adv_grid)
        # 取色按钮行：前景/背景一键取色（qfw ColorDialog）
        color_bar = QHBoxLayout()
        color_bar.setSpacing(8)
        from qfluentwidgets import ToolButton
        self.btn_fg_pick = ToolButton(FluentIcon.PALETTE)
        self.btn_fg_pick.setToolTip(tr("取前景色", "Pick foreground color"))
        self.btn_fg_pick.setAccessibleName(
            tr("取前景色", "Pick foreground color"))
        self.btn_fg_pick.clicked.connect(lambda: self._pick_color(self.ed_fg))
        color_bar.addWidget(self.btn_fg_pick)
        color_bar.addWidget(CaptionLabel(tr("取前景色", "Foreground")))
        color_bar.addSpacing(12)
        self.btn_bg_pick = ToolButton(FluentIcon.PALETTE)
        self.btn_bg_pick.setToolTip(tr("取背景色", "Pick background color"))
        self.btn_bg_pick.setAccessibleName(
            tr("取背景色", "Pick background color"))
        self.btn_bg_pick.clicked.connect(lambda: self._pick_color(self.ed_bg))
        color_bar.addWidget(self.btn_bg_pick)
        color_bar.addWidget(CaptionLabel(tr("取背景色", "Background")))
        color_bar.addStretch(1)
        _holder = QWidget()
        _holder.setLayout(color_bar)
        adv.add_widget(_holder)
        sec_style.add_widget(adv)

        # 预览区（ImageLabel：平滑缩放 + 圆角；外包居中容器——ImageLabel 自绘
        # 忽略 QLabel alignment，靠左显示，须用布局 stretch 水平垂直居中）
        sec_prev = FormSection(tr("预览", "Preview"), FluentIcon.VIEW)
        from qfluentwidgets import ImageLabel
        from PySide6.QtWidgets import (QHBoxLayout as _HL,
                                       QVBoxLayout as _VL, QWidget as _QW)
        self.lb_preview = ImageLabel(
            tr("点击“生成二维码”预览", "Select “Generate QR” to preview"), self)
        self.lb_preview.setMinimumHeight(220)
        self.lb_preview.setBorderRadius(12, 12, 12, 12)
        holder = _QW()
        hv = _VL(holder)
        hv.setContentsMargins(0, 8, 0, 8)
        hv.addStretch(1)
        hh = _HL()
        hh.addStretch(1)
        hh.addWidget(self.lb_preview)
        hh.addStretch(1)
        hv.addLayout(hh)
        hv.addStretch(1)
        holder.setMinimumHeight(260)
        sec_prev.add_widget(holder)
        self.preview_hint = CaptionLabel(tr(
            "保存或打印后请用实际手机试扫。用于分享或印刷时，优先保存为 PNG。",
            "Test the saved or printed code with a real phone. Prefer PNG for sharing or print."))
        self.preview_hint.setWordWrap(True)
        sec_prev.add_widget(self.preview_hint)

        # 编辑器式左右工作区：内容与样式留在左侧，预览始终位于右侧。
        # 生成前无需滚到页面底部寻找结果，符合桌面创作工具的使用习惯。
        workspace = QWidget(self)
        self.workspace_lay = QBoxLayout(QBoxLayout.LeftToRight, workspace)
        self.workspace_lay.setContentsMargins(0, 0, 0, 0)
        self.workspace_lay.setSpacing(14)
        left = QWidget(workspace)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(14)
        left_lay.addWidget(sec)
        left_lay.addWidget(sec_style)
        self.workspace_lay.addWidget(left, 3)
        sec_prev.setMinimumWidth(340)
        self.workspace_lay.addWidget(sec_prev, 2)
        self.preview_section = sec_prev
        lay.addWidget(workspace)

        # 主操作、状态和结果反馈统一进入标题区。
        self.btn_save = PushButton(tr("保存为图片", "Save as image"))
        self.btn_save.clicked.connect(self._save)
        self.btn_save.setEnabled(False)
        self.btn_reset = PushButton(FluentIcon.ROTATE,
                                    tr("恢复默认", "Reset defaults"))
        self.btn_reset.setToolTip(tr(
            "恢复外观/高级参数到默认值（前景色 #000000、背景色 #FFFFFF 等）",
            "Reset appearance to defaults (fg #000000, bg #FFFFFF, ...)"))
        self.btn_reset.clicked.connect(self._reset_defaults)
        self.header.add_action(self.btn_reset)
        self.header.add_action(self.btn_save)
        self.action_bar = ActionBar(tr("生成二维码", "Generate QR"), self)
        self.btn_go = self.action_bar.btn_go
        self.btn_go.clicked.connect(self._generate)
        self.lb_status = self.action_bar.status_label
        lay.addWidget(self.action_bar)

        self._qr_img = None  # PIL Image，保存用（保持引用）
        self._logo_path = None  # 可选 Logo 图片路径
        self._generated_status = ""
        self._status_is_generated = False
        self._bind_dirty_signals()

    # ── 表单辅助 ─────────────────────────────────
    def _combo(self, items, default):
        cb = ComboBox()
        cb.addItems(items)
        cb.setCurrentText(default)
        return cb

    def _make_color_edit(self, text):
        ed = LineEdit()
        ed.setText(text)
        ed.setFixedWidth(90)
        return ed

    def _type_key(self):
        index = self.cb_type.currentIndex()
        return TYPE_KEYS[index if 0 <= index < len(TYPE_KEYS) else 0]

    def _bind_dirty_signals(self):
        """任何影响编码内容或外观的变更都会让旧结果失效。"""
        self.txt_content.textChanged.connect(self._mark_dirty)
        self.ed_ssid.textChanged.connect(self._mark_dirty)
        self.ed_pass.textChanged.connect(self._mark_dirty)
        self.ed_fg.textChanged.connect(self._mark_dirty)
        self.ed_bg.textChanged.connect(self._mark_dirty)
        for combo in (self.cb_type, self.cb_size, self.cb_border,
                      self.cb_style, self.cb_grad):
            combo.currentIndexChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args):
        if self._qr_img is None:
            self.btn_save.setEnabled(False)
            return
        self._qr_img = None
        self._status_is_generated = False
        self.btn_save.setEnabled(False)
        self.action_bar.set_status(
            tr("设置已修改，请重新生成", "Settings changed; generate again"),
            ActionStatusState.WARNING)
        self.preview_hint.setText(tr(
            "当前预览已失效。重新生成后才能保存新设置的二维码。",
            "The preview is out of date. Generate again before saving the new settings."))

    def _sync_type_hint(self):
        """紧贴当前内容类型说明编码规则，避免生成后才发现填写方式不符合预期。"""
        hints = {
            "text": tr(
                "适合短文本和简短说明。内容越短，二维码越容易扫描。",
                "Best for short text. Shorter content produces a code that is easier to scan."),
            "url": tr(
                "仅支持 HTTP 和 HTTPS 链接。省略协议时会自动补全 https://。",
                "HTTP and HTTPS links only. If the protocol is omitted, https:// is added automatically."),
            "wifi": tr(
                "密码留空会生成开放网络二维码。密码会写入二维码，请仅分享给可信用户。",
                "Leave the password empty for an open network. The password is embedded in the code, so share it only with trusted people."),
            "card": tr(
                "使用 vCard 3.0 字段。请保留 BEGIN:VCARD 和 END:VCARD，并修改姓名、电话和邮箱。",
                "Uses vCard 3.0. Keep BEGIN:VCARD and END:VCARD, then edit the name, phone, and email fields."),
        }
        self.type_hint.setText(hints[self._type_key()])

    def _pick_color(self, edit):
        """弹 Qt 原生 QColorDialog 取色并写回输入框。

        为什么弃用 qfw ColorDialog（实测 1.11.3 反复出"选了没变化"）：
        - 依赖遮罩层 + show 淡入 200ms + done 淡出 100ms 动画（异步关闭）；
        - 主类 colorChanged 只在点 OK 且颜色 != oldColor 时发一次；
        - 真实 GUI 环境下取色结果易在动画/时序缝隙丢失。
        Qt 原生 QColorDialog.getColor() 是标准模态对话框，直接返回 QColor，
        取消返回无效色，行为 100% 可预期——功能可靠性优先于 Fluent 外观。
        另注意：DEFAULT_FG 本就是 #000000（标准黑色二维码），未取色时
        输入框显示 #000000 属正常初始状态，并非取色失效。
        """
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        init = QColor(edit.text().strip()) if edit.text().strip() else QColor("#000000")
        if not init.isValid():
            init = QColor("#000000")
        color = QColorDialog.getColor(init, self.window(), tr("选择颜色", "Pick color"))
        if color.isValid():
            edit.setText(color.name())
            self._generate()
            from gui_qt.components import toast
            toast.show_success(
                self,
                tr("已应用颜色 {}", "Color applied {}").format(color.name()))

    # ── 交互 ─────────────────────────────────────
    def _set_wifi_visible(self, visible):
        if hasattr(self, "wifi_holder"):
            self.wifi_holder.setVisible(visible)

    def _on_type_changed(self, t):
        previous = getattr(self, "_active_type_key", "text")
        if previous != "wifi" and hasattr(self, "txt_content"):
            self._drafts[previous] = self.txt_content.toPlainText()
        current = self._type_key()
        self._active_type_key = current
        if current == "wifi":
            self._set_wifi_visible(True)
            self.txt_content.setEnabled(False)
            self.txt_content.setPlainText(
                tr("↓ 请在下方填写 WiFi 名称和密码",
                   "↓ Enter the WiFi name and password below"))
            self.ed_ssid.setFocus()
        else:
            self._set_wifi_visible(False)
            self.txt_content.setEnabled(True)
            self.txt_content.setPlainText(self._drafts[current])
        self._sync_type_hint()

    def _generate(self):
        type_key = self._type_key()
        if type_key == "wifi":
            ssid = self.ed_ssid.text().strip()
            pwd = self.ed_pass.text().strip()
            if not ssid:
                toast.show_warning(self, tr("请输入WiFi名称", "Enter WiFi name"))
                self.ed_ssid.setFocus()
                return
            security = "WPA" if pwd else "nopass"
            content = (
                f"WIFI:T:{security};S:{_escape_wifi(ssid)};"
                f"P:{_escape_wifi(pwd)};;")
        else:
            content = self.txt_content.toPlainText().strip()
            if not content:
                toast.show_warning(self, tr("请输入内容", "Enter content"))
                self.txt_content.setFocus()
                return
            if type_key == "url":
                content = _normalize_url(content)
                if not content:
                    toast.show_warning(
                        self, tr("请输入有效网址，例如 https://example.com",
                                 "Enter a valid URL, such as https://example.com"))
                    self.txt_content.setFocus()
                    return

        try:
            from PySide6.QtGui import QColor
            from core.qr_maker import (make_fancy_qr, GRAD_NONE, GRAD_VERTICAL,
                                       GRAD_DIAGONAL, STYLE_SQUARE,
                                       STYLE_ROUNDED, STYLE_DOT, STYLE_DIAMOND,
                                       RECOMMENDED_MODULE_PIXELS)
            fg = self.ed_fg.text().strip() or DEFAULT_FG
            bg = self.ed_bg.text().strip() or DEFAULT_BG
            if not QColor(fg).isValid() or not QColor(bg).isValid():
                raise ValueError(tr(
                    "颜色格式无效，请使用 #RRGGBB 或标准颜色名",
                    "Invalid color; use #RRGGBB or a standard color name"))
            size = int(self.cb_size.currentText())
            border = int(self.cb_border.currentText())
            style_map = {
                "square": STYLE_SQUARE, "rounded": STYLE_ROUNDED,
                "dot": STYLE_DOT, "diamond": STYLE_DIAMOND,
            }
            grad_map = {
                "none": GRAD_NONE, "vertical": GRAD_VERTICAL,
                "diagonal": GRAD_DIAGONAL,
            }
            img = make_fancy_qr(
                content, size=size, fg=fg, bg=bg,
                style=style_map[STYLE_KEYS[self.cb_style.currentIndex()]],
                gradient=grad_map[GRADIENT_KEYS[self.cb_grad.currentIndex()]],
                logo_path=getattr(self, "_logo_path", None) or "",
                border=border,
                min_module_pixels=RECOMMENDED_MODULE_PIXELS)
        except Exception as e:  # noqa: BLE001
            toast.show_error(
                self, tr("生成失败：{}", "Generation failed: {}").format(e))
            self.action_bar.set_status(
                tr("生成失败", "Failed"), ActionStatusState.ERROR)
            self._status_is_generated = False
            self.preview_hint.setText(tr(
                "未能生成预览。请根据错误提示检查内容、颜色对比度或尺寸。",
                "Preview could not be generated. Check the content, color contrast, or size shown in the error."))
            return

        self._qr_img = img
        self._show_preview(img)
        self.btn_save.setEnabled(True)
        self._generated_status = tr(
            "已生成 {}×{} 二维码", "QR generated {}x{}").format(size, size)
        self._status_is_generated = True
        self._sync_generated_status()
        self.preview_hint.setText(tr(
            "已按建议边距和对比度生成。保存或打印后请用实际手机试扫，包含 Logo 或渐变时更应验证。",
            "Generated with the recommended margin and contrast. Test the saved or printed code with a real phone, especially when using a logo or gradient."))
        self.save_prefs()

    # ── Logo 选择 ─────────────────────────────────
    def _reset_defaults(self):
        """恢复外观/高级参数到出厂默认值（颜色、尺寸、边距、样式、渐变）。

        用户修改前景色/背景色后一键还原；不动内容区（类型/文本/WiFi/Logo）。
        恢复后若有内容立即重新生成预览，并同步保存偏好。
        """
        self.cb_size.setCurrentText("400")
        self.cb_border.setCurrentText("4")
        self.ed_fg.setText(DEFAULT_FG)
        self.ed_bg.setText(DEFAULT_BG)
        self.cb_style.setCurrentIndex(0)
        self.cb_grad.setCurrentIndex(0)
        if self._has_content():
            self._generate()
        self.action_bar.set_status(
            tr("已恢复默认外观", "Appearance reset to defaults"),
            ActionStatusState.IDLE)
        self._status_is_generated = False
        self.save_prefs()
        toast.show_info(self, tr("已恢复默认外观", "Appearance reset to defaults"))

    def _pick_logo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择 Logo 图片", "Pick logo image"), "",
            tr("图片文件 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)",
               "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*.*)"))
        if not path:
            return
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            toast.show_error(
                self, tr("Logo 图片无法读取：{}", "Logo image cannot be read: {}")
                .format(exc))
            return
        self._logo_path = path
        self.lb_logo.setText(tr("Logo：{}", "Logo: {}").format(
            os.path.basename(path)))
        self.btn_logo_clear.setEnabled(True)
        # 有内容时立即重新生成预览
        if self._has_content():
            self._generate()
        else:
            toast.show_info(self, tr("已选择 Logo，填写内容后生成",
                                     "Logo set, fill content then generate"))

    def _clear_logo(self):
        self._logo_path = None
        self.lb_logo.setText(tr("未选择（可选）", "None (optional)"))
        self.btn_logo_clear.setEnabled(False)
        if self._has_content():
            self._generate()

    def _has_content(self) -> bool:
        """当前是否有可生成的内容（WiFi 或文本）。"""
        if self.cb_type.currentText() == "WiFi":
            return bool(self.ed_ssid.text().strip())
        return bool(self.txt_content.toPlainText().strip())

    def _show_preview(self, img):
        """PIL Image → QPixmap 显示到预览区。"""
        try:
            from PIL.ImageQt import ImageQt
            qimg = ImageQt(img)
            pix = QPixmap.fromImage(qimg)
            self.lb_preview.setPixmap(pix.scaled(
                240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception as e:  # noqa: BLE001
            toast.show_error(self, tr("预览失败：{}", "Preview failed: {}").format(e))

    def _save(self):
        if self._qr_img is None:
            toast.show_warning(self, tr("请先生成二维码", "Generate a QR code first"))
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self, tr("保存二维码", "Save QR"), "qrcode.png",
            tr("PNG 图片 (*.png);;JPEG 图片 (*.jpg);;所有文件 (*.*)", "PNG images (*.png);;JPEG images (*.jpg);;All files (*.*)"))
        if not path:
            return
        extension = os.path.splitext(path)[1].lower()
        if not extension:
            extension = ".jpg" if "JPEG" in selected_filter else ".png"
            path += extension
        if extension not in (".png", ".jpg", ".jpeg"):
            toast.show_warning(
                self, tr("仅支持保存为 PNG 或 JPEG 图片",
                         "Save as a PNG or JPEG image"))
            return
        staged_path = None
        try:
            output_dir = os.path.dirname(os.path.abspath(path))
            os.makedirs(output_dir, exist_ok=True)
            fd, staged_path = tempfile.mkstemp(
                prefix=".fm_qrcode_", suffix=extension, dir=output_dir)
            os.close(fd)
            self._qr_img.save(staged_path)
            os.replace(staged_path, path)
            staged_path = None
            self.action_bar.set_status(
                tr("已保存：{}", "Saved: {}").format(os.path.basename(path)),
                ActionStatusState.SUCCESS)
            self._status_is_generated = False
            toast.show_success(
                self, tr("二维码已保存：{}", "QR saved: {}").format(path))
        except Exception as e:  # noqa: BLE001
            toast.show_error(self, tr("保存失败：{}", "Save failed: {}").format(e))
        finally:
            if staged_path:
                try:
                    os.remove(staged_path)
                except OSError:
                    pass

    # ── 参数/偏好（5 键与 tkinter 版一致）─────────
    def collect_params(self) -> dict:
        return {
            "type": self._type_key(),
            "text": self.txt_content.toPlainText().strip(),
            "wifi_ssid": self.ed_ssid.text(),
            "wifi_pass": self.ed_pass.text(),
            "size": self.cb_size.currentText(),
            "border": self.cb_border.currentText(),
            "fg": self.ed_fg.text(),
            "bg": self.ed_bg.text(),
            "style": STYLE_KEYS[self.cb_style.currentIndex()],
            "gradient": GRADIENT_KEYS[self.cb_grad.currentIndex()],
            "logo": getattr(self, "_logo_path", None) or "",
        }

    def collect_prefs(self) -> dict:
        return {
            "qr_type": self._type_key(),
            "qr_size": self.cb_size.currentText(),
            "qr_border": self.cb_border.currentText(),
            "qr_fg": self.ed_fg.text(),
            "qr_bg": self.ed_bg.text(),
            "qr_style": STYLE_KEYS[self.cb_style.currentIndex()],
            "qr_grad": GRADIENT_KEYS[self.cb_grad.currentIndex()],
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        qr_type = prefs.get("qr_type")
        if qr_type in TYPE_KEYS:
            self.cb_type.setCurrentIndex(TYPE_KEYS.index(qr_type))
        elif qr_type in TYPE_VALUES:
            self.cb_type.setCurrentIndex(TYPE_VALUES.index(qr_type))
        if prefs.get("qr_size") in SIZE_VALUES:
            self.cb_size.setCurrentText(prefs["qr_size"])
        border = prefs.get("qr_border")
        self.cb_border.setCurrentText(border if border in BORDER_VALUES else "4")
        if prefs.get("qr_fg"):
            self.ed_fg.setText(prefs["qr_fg"])
        if prefs.get("qr_bg"):
            self.ed_bg.setText(prefs["qr_bg"])
        style = prefs.get("qr_style")
        if style in STYLE_KEYS:
            self.cb_style.setCurrentIndex(STYLE_KEYS.index(style))
        elif isinstance(style, int) and 0 <= style < self.cb_style.count():
            self.cb_style.setCurrentIndex(style)
        gradient = prefs.get("qr_grad")
        if gradient in GRADIENT_KEYS:
            self.cb_grad.setCurrentIndex(GRADIENT_KEYS.index(gradient))
        elif isinstance(gradient, int) and 0 <= gradient < self.cb_grad.count():
            self.cb_grad.setCurrentIndex(gradient)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layout = getattr(self, "workspace_lay", None)
        if layout is None:
            return
        narrow = self.viewport().width() < 900
        layout.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.preview_section.setMinimumWidth(0 if narrow else 340)
        self.style_grid.set_columns(1 if narrow else 3)
        self.wifi_grid.set_columns(1 if narrow else 2)
        self.btn_reset.setText(
            tr("重置", "Reset") if narrow else tr("恢复默认", "Reset defaults"))
        self.btn_save.setText(
            tr("保存", "Save") if narrow else tr("保存为图片", "Save as image"))
        self.btn_go.setText(
            tr("生成", "Generate") if narrow else tr("生成二维码", "Generate QR"))
        # 当前页的窄屏状态只显示简短结果，放宽 ActionBar
        # 的通用最小宽度会造成少量水平滚动，因此仅在本页收紧。
        self.lb_status.setMinimumWidth(40 if narrow else 72)
        self.lb_status.setMaximumWidth(120 if narrow else 280)
        if self._status_is_generated:
            self._sync_generated_status()

    def _sync_generated_status(self):
        narrow = self.viewport().width() < 900
        text = tr("已生成", "Generated") if narrow else self._generated_status
        self.action_bar.set_status(text, ActionStatusState.SUCCESS)
        self.lb_status.setToolTip(self._generated_status)
