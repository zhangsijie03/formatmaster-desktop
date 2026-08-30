"""pdf_panel — PDF 工具面板（阶段2 迁移自 gui/panels/pdf_panel.py）。

9 种操作模式：合并/拆分/按页提取/加密/解密/压缩/添加水印/添加页码/转为图片。
各模式独立子区，随模式切换显隐；任务经 TaskManager 通用链路执行
core.tools / core.pdf_extract / core.pdf_to_image（不依赖 FFmpeg）。
顶部入口按钮可跳转到 PDF编辑（pdf_editor_panel）可视化编辑页。
"""
import os
from collections.abc import Sequence

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (FluentIcon, CaptionLabel, ComboBox,
                            LineEdit, PasswordLineEdit, PushButton,
                            SegmentedWidget)

from core.tools import (pdf_add_page_numbers, pdf_add_watermark, pdf_compress,
                        pdf_decrypt, pdf_encrypt, pdf_merge, pdf_split)
from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

MODE_VALUES = [
    tr("合并（多个→一个）", "Merge (many→one)"),
    tr("拆分（一个→多个）", "Split (one→many)"),
    tr("按页提取", "Extract pages"),
    tr("加密（设置密码）", "Encrypt (set password)"),
    tr("解密（移除密码）", "Decrypt (remove password)"),
    tr("压缩", "Compress"),
    tr("添加水印", "Add watermark"),
    tr("添加页码", "Add page numbers"),
    tr("转为图片", "To image"),
]

# 模式分段选择器：routeKey(完整模式名) → 短标签（SegmentedWidget 展示）
MODE_SHORT_LABELS = [
    (tr("合并（多个→一个）", "Merge (many→one)"), tr("合并", "Merge")),
    (tr("拆分（一个→多个）", "Split (one→many)"), tr("拆分", "Split")),
    (tr("按页提取", "Extract pages"), tr("按页提取", "Extract pages")),
    (tr("压缩", "Compress"), tr("压缩", "Compress")),
    (tr("转为图片", "To image"), tr("转图片", "To image")),
    (tr("加密（设置密码）", "Encrypt (set password)"), tr("加密", "Encrypt")),
    (tr("解密（移除密码）", "Decrypt (remove password)"), tr("解密", "Decrypt")),
    (tr("添加水印", "Add watermark"), tr("水印", "Watermark")),
    (tr("添加页码", "Add page numbers"), tr("页码", "Page numbers")),
]

MODE_HINTS = dict(zip(MODE_VALUES, [
    tr("按文件列表顺序合并为一个 PDF，至少需要 2 个文件。", "Merge in file-list order into one PDF; at least 2 files are required."),
    tr("按页码范围拆分，每个范围输出一个 PDF。", "Split each page range into a separate PDF."),
    tr("选择提取方式；每页一个文件时无需填写页码范围。", "Choose an extraction mode; one-file-per-page does not need a range."),
    tr("至少设置一个密码；打开密码用于阅读，权限密码用于限制修改。", "Set at least one password: open password for reading, owner password for permissions."),
    tr("输入源文件密码，为每个文件生成移除密码后的 PDF。", "Enter the source password to create an unencrypted PDF for each file."),
    tr("降低分辨率或图片质量可减小体积，也可能影响清晰度。", "Lower resolution or image quality may reduce size and sharpness."),
    tr("为每个 PDF 添加文字水印，原文件不直接修改。", "Add a text watermark to each PDF without editing the source directly."),
    tr("页码格式中的 {n} 会替换为页码。", "{n} in the number format is replaced with the page number."),
    tr("每页生成一张图片；页码范围留空时导出全部页面。", "Export one image per page; leave the range blank to export all pages."),
]))


def _parse_ranges(range_str):
    """「1-3,5,7-10」→ [(1,3),(5,5),(7,10)]（与 tkinter 版 _go 一致）。"""
    ranges = []
    for part in (range_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            s, e = part.split("-", 1)
            ranges.append((int(s.strip()), int(e.strip())))
        else:
            ranges.append((int(part), int(part)))
    return ranges


class PdfPanelPage(BaseQtPanel, TaskPanelMixin):
    """PDF 工具页。"""

    panel_key = "pdf"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        # 密码只在当前进程内复用。旧版本曾将历史明文写入偏好；首次进入
        # PDF 面板时主动清空遗留值，避免敏感信息继续留在磁盘或备份中。
        self._session_pwd_history = []
        if self.services.prefs.get("qt_app", self._PWD_HIST_KEY):
            setter = getattr(self.services.prefs, "set_now", None) or \
                getattr(self.services.prefs, "set", None)
            if setter:
                setter("qt_app", self._PWD_HIST_KEY, [])
        lay = self.content_layout
        self.header = PageHeader(
            tr("PDF 处理", "PDF tools"),
            tr("合并、拆分、提取、压缩、保护与导出图片",
               "Merge, split, extract, compress, protect, and export images"),
            FluentIcon.DOCUMENT)
        lay.addWidget(self.header)

        # 预览与编辑都是页面级上下文命令，与主执行组统一放在标题区域。
        self.btn_preview = PushButton(FluentIcon.VIEW, tr("预览", "Preview"))
        self.btn_preview.setToolTip(
            tr("预览选中的 PDF，未选择时打开第一份", "Preview the selected PDF, or the first file if none is selected"))
        self.btn_preview.clicked.connect(self._preview_pdf)
        self.btn_preview.setEnabled(False)
        self.header.add_action(self.btn_preview)
        self.btn_editor = PushButton(FluentIcon.EDIT,
                                     tr("可视化编辑", "Visual editor"))
        self.btn_editor.clicked.connect(self._open_editor_page)
        self.header.add_action(self.btn_editor)

        self.file_card = FileListCard(tr("文件列表", "File list"), file_exts={".pdf"})
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt("PDF")

        lay.addWidget(self._build_settings_card())

        from gui_qt.components.form_widgets import FormSection
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)

        self.file_card.files_changed.connect(self._sync_context_actions)
        self.file_card.table.itemSelectionChanged.connect(self._sync_context_actions)
        self._mode_changed()
        self._wire_tasks()
        self._sync_context_actions()

    def _build_settings_card(self):
        from gui_qt.components.form_widgets import FormSection

        sec = FormSection(tr("操作设置", "Options"), FluentIcon.SETTING)

        # 宽屏沿用分段选择器；窄屏改用同一套组件库的下拉框，两个入口共用模式值。
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(CaptionLabel(tr("操作模式", "Mode")))
        self.cb_mode = SegmentedWidget()
        for full, label in MODE_SHORT_LABELS:
            self.cb_mode.addItem(full, label)
        self.cb_mode.setCurrentItem(tr("合并（多个→一个）", "Merge (many→one)"))
        self.cb_mode.currentItemChanged.connect(
            lambda _key: self._mode_changed())
        mode_row.addWidget(self.cb_mode, 1)
        self.cb_mode_compact = ComboBox()
        self.cb_mode_compact.addItems(MODE_VALUES)
        self.cb_mode_compact.currentTextChanged.connect(self._compact_mode_changed)
        self.cb_mode_compact.hide()
        mode_row.addWidget(self.cb_mode_compact, 1)
        sec.add_layout(mode_row)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)
        self._parameter_grids = []

        # 各模式子区随当前 Tab 直接切换。模式本身已经是一级导航，
        # 不再叠加“高级选项”开关形成重复层级。
        self.sec_split = self._build_split_section()
        self.sec_encrypt = self._build_encrypt_section()
        self.sec_decrypt = self._build_decrypt_section()
        self.sec_compress = self._build_compress_section()
        self.sec_wm = self._build_watermark_section()
        self.sec_pn = self._build_page_number_section()
        self.sec_img = self._build_to_image_section()
        for w in (self.sec_split, self.sec_encrypt, self.sec_decrypt,
                  self.sec_compress, self.sec_wm, self.sec_pn, self.sec_img):
            sec.add_widget(w)
        return sec

    def _form_widget(self, fields: Sequence[tuple[str, QWidget]]) -> QWidget:
        """复用统一 FormGrid，仅让本页参数随窗口宽度重排。"""
        from gui_qt.components.form_widgets import FormGrid
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        grid = FormGrid(columns=2)
        for label, control in fields:
            grid.add_field(label, control)
        layout.addLayout(grid)
        self._parameter_grids.append(grid)
        return w

    @staticmethod
    def _combo(items: Sequence[str], default: str) -> ComboBox:
        control = ComboBox()
        control.addItems(items)
        control.setCurrentText(default)
        return control

    @staticmethod
    def _password_field(editor: PasswordLineEdit, history: ComboBox) -> QWidget:
        field = QWidget()
        layout = QHBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(editor, 1)
        layout.addWidget(history)
        return field

    def _build_split_section(self):
        self.ed_range = LineEdit()
        self.ed_range.setPlaceholderText(tr("例如 1-3,5,7-10", "e.g. 1-3,5,7-10"))
        self.cb_extract_mode = self._combo([
            tr("按范围提取", "By range"), tr("每页一个文件", "One file per page"),
            tr("指定页码", "Specific pages")], tr("按范围提取", "By range"))
        self.cb_extract_mode.currentIndexChanged.connect(self._sync_range_controls)
        return self._form_widget([
            (tr("页码范围", "Page range"), self.ed_range),
            (tr("提取方式", "Extraction mode"), self.cb_extract_mode)])

    def _build_encrypt_section(self):
        self.ed_open_pwd = PasswordLineEdit()
        self.ed_owner_pwd = PasswordLineEdit()
        self.ed_open_pwd.setAccessibleName(tr("打开密码", "Open password"))
        self.ed_owner_pwd.setAccessibleName(tr("权限密码", "Owner password"))
        self.cb_open_hist = self._hist_combo(self.ed_open_pwd)
        self.cb_owner_hist = self._hist_combo(self.ed_owner_pwd)
        self.cb_encrypt_method = self._combo(["AES-256", "AES-128"], "AES-256")
        return self._form_widget([
            (tr("打开密码", "Open password"), self._password_field(self.ed_open_pwd, self.cb_open_hist)),
            (tr("权限密码", "Owner password"), self._password_field(self.ed_owner_pwd, self.cb_owner_hist)),
            (tr("加密方式", "Encryption"), self.cb_encrypt_method)])

    def _build_decrypt_section(self):
        self.ed_decrypt_pwd = PasswordLineEdit()
        self.ed_decrypt_pwd.setAccessibleName(tr("输入密码", "Password"))
        self.cb_decrypt_hist = self._hist_combo(self.ed_decrypt_pwd)
        return self._form_widget([(tr("输入密码", "Password"),
            self._password_field(self.ed_decrypt_pwd, self.cb_decrypt_hist))])

    # ── PDF 会话密码历史 ──────────────────────────
    # 最近 10 条密码仅保留在内存，退出程序立即消失，禁止写入偏好或备份。
    _PWD_HIST_KEY = "pdf_pwd_history"
    _PWD_HIST_MAX = 10

    def _pwd_hist(self):
        h = getattr(self, "_session_pwd_history", [])
        if not isinstance(h, (list, tuple)):
            return []
        return [p for p in h if isinstance(p, str)]

    def _record_pwd(self, pwd):
        """记录一条会话密码（去重置顶，最多 10 条）。后台线程可调用。

        注意：只写偏好，不碰 UI（Qt 控件非线程安全）；历史下拉的刷新
        由主线程 _on_state 任务成功回调执行。
        """
        if not pwd:
            return
        h = [p for p in self._pwd_hist() if p != pwd]
        h.insert(0, pwd)
        self._session_pwd_history = h[:self._PWD_HIST_MAX]

    def _refresh_hist_combos(self):
        """重建全部历史密码下拉（主线程调用：任务成功回调/面板重进）。"""
        hist = self._pwd_hist()
        for cb in (getattr(self, "cb_open_hist", None),
                   getattr(self, "cb_owner_hist", None),
                   getattr(self, "cb_decrypt_hist", None)):
            if cb is None:
                continue
            cb.blockSignals(True)
            try:
                cb.clear()
                cb.addItem(tr("历史…", "History"))
                for p in hist:
                    cb.addItem(p)
                cb.setCurrentIndex(0)
            finally:
                cb.blockSignals(False)

    def _on_state(self, task_id, state):
        """任务状态回调（主线程）：加密/解密成功后刷新历史下拉。"""
        super()._on_state(task_id, state)
        if state == "success":
            try:
                self._refresh_hist_combos()
            except Exception:  # noqa: BLE001 - 刷新失败不影响任务
                pass

    def _hist_combo(self, target):
        """历史密码下拉：选中即填入目标密码框（选中后自动复位）。"""
        cb = ComboBox()
        cb.setFixedWidth(92)
        cb.addItem(tr("历史…", "History"))
        for p in self._pwd_hist():
            cb.addItem(p)
        cb.setCurrentIndex(0)

        def _on(idx):
            if idx <= 0:
                return
            target.setText(cb.itemText(idx))
            cb.setCurrentIndex(0)  # 复位，避免重复触发
        cb.currentIndexChanged.connect(_on)
        return cb

    def _build_compress_section(self):
        self.cb_compress_dpi = self._combo(["72dpi", "100dpi", "150dpi", "200dpi"], "150dpi")
        self.cb_compress_quality = self._combo(["60", "70", "80", "90"], "80")
        return self._form_widget([
            (tr("目标分辨率", "Target resolution"), self.cb_compress_dpi),
            (tr("图片质量", "Image quality"), self.cb_compress_quality)])

    def _build_watermark_section(self):
        self.ed_wm_text = LineEdit()
        self.ed_wm_text.setText(tr("机密", "Confidential"))
        self.cb_wm_pos = self._combo([
            tr("左上角", "Top left"), tr("右上角", "Top right"),
            tr("左下角", "Bottom left"), tr("右下角", "Bottom right"),
            tr("居中", "Center")], tr("居中", "Center"))
        self.cb_wm_opacity = self._combo(["0.1", "0.2", "0.3", "0.5", "0.7", "0.9"], "0.3")
        self.cb_wm_rotate = self._combo(["0°", "45°", "90°"], "0°")
        return self._form_widget([
            (tr("水印文字", "Watermark text"), self.ed_wm_text),
            (tr("位置", "Position"), self.cb_wm_pos),
            (tr("透明度", "Opacity"), self.cb_wm_opacity),
            (tr("旋转", "Rotate"), self.cb_wm_rotate)])

    def _build_page_number_section(self):
        self.ed_pn_start = LineEdit()
        self.ed_pn_start.setText("1")
        self.ed_pn_start.setValidator(QIntValidator(1, 999999, self))
        self.cb_pn_pos = self._combo([
            tr("底部居中", "Bottom center"), tr("底部左对齐", "Bottom left"),
            tr("底部右对齐", "Bottom right"), tr("顶部居中", "Top center")],
            tr("底部居中", "Bottom center"))
        self.ed_pn_fmt = LineEdit()
        self.ed_pn_fmt.setText(tr("第{n}页", "Page {n}"))
        return self._form_widget([
            (tr("起始页码", "Start number"), self.ed_pn_start),
            (tr("位置", "Position"), self.cb_pn_pos),
            (tr("格式（{n}=页码）", "Format ({n}=page)"), self.ed_pn_fmt)])

    def _build_to_image_section(self):
        self.cb_img_fmt = self._combo(["PNG", "JPG"], "PNG")
        self.cb_img_dpi = self._combo(["72", "100", "150", "200", "300", "400", "600"], "200")
        self.ed_img_pages = LineEdit()
        self.ed_img_pages.setPlaceholderText(tr("留空=全部  示例: 1-3,5,7", "blank=all   e.g. 1-3,5,7"))
        return self._form_widget([
            (tr("图片格式", "Image format"), self.cb_img_fmt),
            (tr("输出 DPI", "Output DPI"), self.cb_img_dpi),
            (tr("页码范围", "Page range"), self.ed_img_pages)])

    # ── 模式切换 ─────────────────────────────────
    def _mode_changed(self):
        mode = self.cb_mode.currentRouteKey()
        self.cb_mode_compact.blockSignals(True)
        self.cb_mode_compact.setCurrentText(mode)
        self.cb_mode_compact.blockSignals(False)
        self.mode_hint.setText(MODE_HINTS.get(mode, ""))
        secs = (self.sec_split, self.sec_encrypt, self.sec_decrypt,
                self.sec_compress, self.sec_wm, self.sec_pn, self.sec_img)
        for w in secs:
            w.setVisible(False)
        if tr("拆分", "Split") in mode or tr("提取", "Extract") in mode:
            self.sec_split.setVisible(True)
        elif tr("加密", "Encrypt") in mode:
            self.sec_encrypt.setVisible(True)
        elif tr("解密", "Decrypt") in mode:
            self.sec_decrypt.setVisible(True)
        elif tr("压缩", "Compress") in mode:
            self.sec_compress.setVisible(True)
        elif tr("水印", "Watermark") in mode:
            self.sec_wm.setVisible(True)
        elif tr("页码", "Page numbers") in mode:
            self.sec_pn.setVisible(True)
        elif tr("转为图片", "To image") in mode:
            self.sec_img.setVisible(True)
        self._sync_range_controls()
        self._sync_context_actions()

    def _compact_mode_changed(self, mode: str) -> None:
        if mode in MODE_VALUES:
            self.cb_mode.setCurrentItem(mode)
            self._mode_changed()

    def _sync_range_controls(self, *_args) -> None:
        """拆分不使用提取方式；逐页提取不使用范围，保留输入以便切回。"""
        extracting = self.cb_mode.currentRouteKey() == MODE_VALUES[2]
        self.cb_extract_mode.setEnabled(extracting)
        self.ed_range.setEnabled(not extracting or self.cb_extract_mode.currentIndex() != 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.viewport().width() < 1000
        if hasattr(self, "cb_mode_compact"):
            self.cb_mode.setVisible(not narrow)
            self.cb_mode_compact.setVisible(narrow)
        for grid in getattr(self, "_parameter_grids", []):
            grid.set_columns(1 if self.viewport().width() < 820 else 2)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "mode": self.cb_mode.currentRouteKey(),
            "range": self.ed_range.text().strip(),
            "extract_mode": self.cb_extract_mode.currentText(),
            "open_pwd": self.ed_open_pwd.text(),
            "owner_pwd": self.ed_owner_pwd.text(),
            "encrypt_method": self.cb_encrypt_method.currentText(),
            "decrypt_pwd": self.ed_decrypt_pwd.text(),
            "compress_dpi": self.cb_compress_dpi.currentText(),
            "compress_quality": self.cb_compress_quality.currentText(),
            "wm_text": self.ed_wm_text.text().strip(),
            "wm_pos": self.cb_wm_pos.currentText(),
            "wm_pos_index": self.cb_wm_pos.currentIndex(),
            "wm_opacity": float(self.cb_wm_opacity.currentText()),
            "wm_rotate": int(self.cb_wm_rotate.currentText().replace("°", "")),
            "pn_start": int(self.ed_pn_start.text())
            if self.ed_pn_start.text().isdigit() else 1,
            "pn_pos": self.cb_pn_pos.currentText(),
            "pn_pos_index": self.cb_pn_pos.currentIndex(),
            "pn_fmt": self.ed_pn_fmt.text(),
            "to_image_fmt": self.cb_img_fmt.currentText(),
            "to_image_dpi": int(self.cb_img_dpi.currentText()),
            "to_image_pages": self.ed_img_pages.text().strip(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        # 与 tkinter 版一致：仅持久化 mode + 输出目录
        return {
            "mode": self.cb_mode.currentRouteKey(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("mode") in MODE_VALUES:
            self.cb_mode.setCurrentItem(prefs["mode"])
            self._mode_changed()
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        mode = p.get("mode", "")
        files_list = p.get("files") or [task.file_path]

        if tr("合并", "Merge") in mode:
            return pdf_merge(files_list, task.output_path, prog)
        if tr("拆分", "Split") in mode:
            ranges = _parse_ranges(p.get("range", ""))
            if not ranges:
                task.error = tr("页码范围无效", "Invalid page range")
                return False
            return pdf_split(task.file_path, task.output_path, ranges, prog)
        if tr("提取", "Extract") in mode:
            from core.pdf_extract import pdf_extract_pages
            # 提取模式判定需兼容中英文选项（"每页一个文件"/"One file per page" 等）
            em = (p.get("extract_mode", "") or "").lower()
            if "each" in em or "per page" in em or "每页" in em:
                ex_mode = "each"
            elif "selected" in em or "specific" in em or "指定" in em:
                ex_mode = "selected"
            else:
                ex_mode = "range"
            return pdf_extract_pages(task.file_path, task.output_path, ex_mode,
                                     p.get("range", ""), prog)
        if tr("加密", "Encrypt") in mode:
            if not p.get("open_pwd") and not p.get("owner_pwd"):
                task.error = tr("请至少设置一个密码", "Set at least one password")
                return False
            ok = pdf_encrypt(task.file_path, task.output_path,
                             p.get("open_pwd", ""), p.get("owner_pwd", ""),
                             p.get("encrypt_method", "AES-256"), prog)
            if ok:
                # 加密成功：记录密码到历史（最近 10 条，一键复用）
                self._record_pwd(p.get("open_pwd", ""))
                self._record_pwd(p.get("owner_pwd", ""))
            return ok
        if tr("解密", "Decrypt") in mode:
            ok = pdf_decrypt(task.file_path, task.output_path,
                             p.get("decrypt_pwd", ""), prog)
            if ok:
                self._record_pwd(p.get("decrypt_pwd", ""))
            return ok
        if tr("压缩", "Compress") in mode:
            dpi = int(p.get("compress_dpi", "150dpi").replace("dpi", ""))
            quality = int(p.get("compress_quality", "80"))
            return pdf_compress(task.file_path, task.output_path, dpi, quality, prog)
        if tr("水印", "Watermark") in mode:
            if not p.get("wm_text"):
                task.error = tr("水印文字不能为空", "Watermark text cannot be empty")
                return False
            positions = ["左上角", "右上角", "左下角", "右下角", "居中"]
            pos = positions[max(0, min(p.get("wm_pos_index", 4), 4))]
            return pdf_add_watermark(task.file_path, task.output_path,
                                     text=p["wm_text"],
                                     pos=pos,
                                     opacity=p.get("wm_opacity", 0.3),
                                     rotation=p.get("wm_rotate", 0),
                                     progress_cb=prog)
        if tr("页码", "Page numbers") in mode:
            positions = ["底部居中", "底部左对齐", "底部右对齐", "顶部居中"]
            pos = positions[max(0, min(p.get("pn_pos_index", 0), 3))]
            return pdf_add_page_numbers(task.file_path, task.output_path,
                                        start=p.get("pn_start", 1),
                                        pos=pos,
                                        fmt=p.get("pn_fmt", "{n}"),
                                        progress_cb=prog)
        if tr("转为图片", "To image") in mode:
            from core.pdf_to_image import pdf_to_images
            ok, _saved = pdf_to_images(task.file_path, task.output_path,
                                       fmt=p.get("to_image_fmt", "PNG"),
                                       dpi=p.get("to_image_dpi", 200),
                                       pages=p.get("to_image_pages", ""),
                                       progress_cb=prog)
            return ok
        task.error = tr("未知操作模式", "Unknown mode")
        return False

    # ── 任务提交（合并为整批单任务）────────────────
    def _start(self):
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加 PDF 文件", "Add PDF files first"))
            return
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return

        mode = self.cb_mode.currentRouteKey()
        params = self.collect_params()
        if tr("合并", "Merge") in mode and len(files) < 2:
            toast.show_warning(
                self, tr("合并至少需要 2 个 PDF 文件",
                         "Merge requires at least two PDF files"))
            return False
        needs_range = (tr("拆分", "Split") in mode
                       or (tr("提取", "Extract") in mode
                           and self.cb_extract_mode.currentIndex() != 1))
        if needs_range:
            try:
                ranges = _parse_ranges(params["range"])
                if not ranges or any(start < 1 or end < start
                                     for start, end in ranges):
                    raise ValueError
            except ValueError:
                toast.show_warning(
                    self, tr("请输入有效页码范围，例如 1-3,5,7-10",
                             "Enter a valid page range, e.g. 1-3,5,7-10"))
                return False
        if tr("水印", "Watermark") in mode and not params["wm_text"]:
            toast.show_warning(self, tr("水印文字不能为空", "Watermark text cannot be empty"))
            return False
        if (tr("加密", "Encrypt") in mode
                and not params["open_pwd"] and not params["owner_pwd"]):
            toast.show_warning(
                self, tr("请至少设置一个密码", "Set at least one password"))
            return False
        self.save_prefs()
        mgr = self.services.task_manager

        if tr("合并", "Merge") in mode:
            out_dir = self.out_row.resolve_dir(files[0])
            out_path = self._unique_path(os.path.join(out_dir, "merged.pdf"))
            params["files"] = list(files)
            tid = mgr.add_task(
                name=tr("PDF合并 - {}个文件", "PDF Merge - {} files").format(len(files)),
                task_type="pdf", file_path=files[0], output_path=out_path,
                params=params, runner=self._runner,
                history_type=tr("PDF 处理", "PDF Tools"), history_target=tr("合并", "Merge"),
                need_ffmpeg=False)
            if tid is not None:
                self._task_rows[tid] = (files[0], -1)
                self.action_bar.set_running(True)
                self.action_bar.set_status(tr("已提交合并任务", "Merge task submitted"))
            return True

        # 其余模式：逐文件入队
        added = 0
        for f in files:
            kwargs = self._make_task(f)
            if kwargs is None:
                continue
            tid = mgr.add_task(**kwargs)
            if tid is not None:
                self._task_rows[tid] = (f, self.file_card.row_of_file(f))
                added += 1
        if added:
            self.action_bar.set_running(True)
            self.action_bar.set_status(tr("已提交 {} 个任务", "Submitted {} tasks").format(added))
        else:
            toast.show_error(self, tr("任务提交失败", "Submit failed"))
        return bool(added)

    def _sync_context_actions(self):
        """同步预览和主执行状态，避免无文件或单文件合并仍可点击。"""
        files = self.file_card.files() if hasattr(self, "file_card") else []
        if hasattr(self, "btn_preview"):
            self.btn_preview.setEnabled(bool(files))
            selected = self._selected_pdf_file()
            self.btn_preview.setToolTip(
                tr("预览：{}", "Preview: {}").format(os.path.basename(selected))
                if selected else tr("请先添加 PDF 文件", "Add PDF files first"))
        if not hasattr(self, "action_bar"):
            return
        TaskPanelMixin._sync_start_enabled(self)
        mode = self.cb_mode.currentRouteKey() if hasattr(self, "cb_mode") else ""
        if tr("合并", "Merge") in mode and len(files) < 2:
            self.action_bar.btn_go.setEnabled(False)
            self.action_bar.btn_go.setToolTip(
                tr("合并至少需要 2 个 PDF 文件",
                   "Merge requires at least two PDF files"))

    def _sync_start_enabled(self):
        """任务状态变化后也保留 PDF 模式自身的提交约束。"""
        self._sync_context_actions()

    def _unique_path(self, path):
        """已存在时追加 _N 计数（与 make_output_path 行为一致）。"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(f"{base}_{counter}{ext}"):
            counter += 1
        return f"{base}_{counter}{ext}"

    def _make_task(self, f):
        params = self.collect_params()
        mode = params["mode"]
        nm = os.path.splitext(os.path.basename(f))[0]
        out_dir = self.out_row.resolve_dir(f)
        if tr("拆分", "Split") in mode:
            out_path = os.path.join(out_dir, nm + "_split")
            try:
                os.makedirs(out_path, exist_ok=True)
            except OSError:
                toast.show_error(self, f"无法创建输出目录：{out_path}")
                return None
        elif tr("提取", "Extract") in mode:
            out_path = os.path.join(out_dir, nm + "_extract")
            try:
                os.makedirs(out_path, exist_ok=True)
            except OSError:
                toast.show_error(self, f"无法创建输出目录：{out_path}")
                return None
        elif tr("转为图片", "To image") in mode:
            out_path = os.path.join(out_dir, nm + "_images")
            try:
                os.makedirs(out_path, exist_ok=True)
            except OSError:
                toast.show_error(self, f"无法创建输出目录：{out_path}")
                return None
        elif tr("加密", "Encrypt") in mode:
            out_path = self._unique_path(os.path.join(out_dir, nm + "_encrypted.pdf"))
        elif tr("解密", "Decrypt") in mode:
            out_path = self._unique_path(os.path.join(out_dir, nm + "_decrypted.pdf"))
        elif tr("压缩", "Compress") in mode:
            out_path = self._unique_path(os.path.join(out_dir, nm + "_compressed.pdf"))
        else:
            out_path = self._unique_path(os.path.join(out_dir, nm + "_numbered.pdf"
                                                      if tr("页码", "Page numbers") in mode
                                                      else nm + "_watermarked.pdf"))
        return dict(
            name=f"{tr('PDF处理', 'PDF Tools')} - {os.path.basename(f)}",
            task_type="pdf", file_path=f, output_path=out_path,
            params=params, runner=self._runner,
            history_type=tr("PDF 处理", "PDF Tools"), history_target=mode.split("（")[0],
            need_ffmpeg=False)

    def _preview_pdf(self):
        """内置 QtPdf 预览选中的 PDF。"""
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加 PDF 文件", "Add PDF files first"))
            return
        from gui_qt.components.pdf_preview import PdfPreviewDialog
        dlg = PdfPreviewDialog(self._selected_pdf_file(), self.window())
        dlg.exec()

    def _selected_pdf_file(self) -> str:
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        if rows and 0 <= rows[0].row() < len(files):
            return files[rows[0].row()]
        return files[0] if files else ""

    def _open_editor_page(self):
        """跳转到 PDF编辑 导航页（对应 tkinter 版 _open_pdf_editor 弹窗）。"""
        # 优先用 MainWindow 挂载的 _switch_to(key)，回退到标准 switchTo
        page = getattr(self.main_window, "pages", {}).get("pdf_editor")
        if page is None:
            return
        switcher = getattr(self.main_window, "_switch_to", None)
        if callable(switcher):
            switcher("pdf_editor")
        else:
            self.main_window.switchTo(page)

    def _empty_hint(self):
        return tr("请先添加 PDF 文件", "Add PDF files first")
