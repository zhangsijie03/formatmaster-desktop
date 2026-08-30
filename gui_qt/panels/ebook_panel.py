# -*- coding: utf-8 -*-
"""ebook_panel — 电子书互转面板。

EPUB / MOBI / PRC / AZW / AZW3 / TXT / HTML 互转（core.ebook_converter）。
MOBI/AZW3 目标格式需要系统安装 Calibre（自动探测 ebook-convert）；
EPUB/TXT/HTML 互转完全离线、零依赖。
"""
import os

from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PushButton

from core.ebook_converter import SRC_EXTS, convert_ebook, _find_calibre_convert
from gui_qt import task_manager as tm
from gui_qt.components import toast
from gui_qt.i18n import tr
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

DST_VALUES = ["EPUB", "TXT", "HTML", "MOBI", "AZW3"]
# 目标扩展名映射（HTML 源允许 .htm）
_DST_EXT = {"EPUB": ".epub", "MOBI": ".mobi", "AZW3": ".azw3",
            "TXT": ".txt", "HTML": ".html"}
CALIBRE_TARGETS = frozenset({"MOBI", "AZW3"})
CALIBRE_SOURCE_EXTS = frozenset({".azw3"})
SOURCE_EXT_ALIASES = {".htm": ".html"}
TARGET_HINTS = {
    "EPUB": tr("EPUB：供电子书阅读器打开，转换后排版可能变化。",
               "EPUB: for ebook readers; layout may change during conversion."),
    "TXT": tr("TXT：只保留文字，不保留图片、封面和排版。",
              "TXT: text only; images, covers and formatting are not retained."),
    "HTML": tr("HTML：输出单个网页文件，可用浏览器查看。",
               "HTML: exports a single webpage for viewing in a browser."),
    "MOBI": tr("MOBI：由本机 Calibre 生成电子书文件。",
               "MOBI: creates the ebook using your local Calibre installation."),
    "AZW3": tr("AZW3：由本机 Calibre 生成电子书文件。",
               "AZW3: creates the ebook using your local Calibre installation."),
}


class EbookPanelPage(BaseQtPanel, TaskPanelMixin):
    """电子书互转页。"""

    panel_key = "ebook"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("电子书互转", "Ebook conversion"),
            tr("EPUB、MOBI、AZW3、TXT 与 HTML 互转，本地处理不上传",
               "Convert EPUB, MOBI, AZW3, TXT, and HTML locally"),
            FluentIcon.LIBRARY))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=SRC_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt("EPUB")

        from gui_qt.components.form_widgets import FormSection, FormGrid
        sec = FormSection(tr("转换参数", "Conversion settings"), FluentIcon.LIBRARY)
        self.params_grid = FormGrid(columns=2)
        self.cb_dst = ComboBox()
        self.cb_dst.addItems(DST_VALUES)
        self.cb_dst.setCurrentText("EPUB")
        self.params_grid.add_field(
            tr("目标格式", "Target format"), self.cb_dst,
            hint=tr("EPUB/TXT/HTML 离线转换；MOBI/AZW3 自动调用 Calibre",
                    "EPUB/TXT/HTML offline; MOBI/AZW3 via Calibre"))
        self.engine_label = CaptionLabel("")
        self.engine_label.setWordWrap(True)
        self.engine_label.setProperty("sec", True)
        self.params_grid.add_field(
            tr("转换引擎", "Conversion engine"), self.engine_label,
            hint=tr("Calibre 安装后会被自动识别",
                    "Calibre is detected automatically when installed"))
        sec.add_form(self.params_grid)
        self.target_hint = CaptionLabel()
        self.target_hint.setWordWrap(True)
        sec.add_widget(self.target_hint)

        self.engine_actions = QWidget()
        engine_row = QHBoxLayout(self.engine_actions)
        engine_row.setContentsMargins(0, 0, 0, 0)
        engine_row.setSpacing(8)
        self.btn_recheck = PushButton(FluentIcon.SYNC, tr("重新检测", "Recheck"))
        self.btn_recheck.clicked.connect(self._on_target_changed)
        engine_row.addWidget(self.btn_recheck)
        self.engine_hint = CaptionLabel()
        self.engine_hint.setWordWrap(True)
        engine_row.addWidget(self.engine_hint, 1)
        sec.add_widget(self.engine_actions)
        lay.addWidget(sec)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始转换", "Convert"))
        lay.addWidget(self.action_bar)

        self.cb_dst.currentTextChanged.connect(self._on_target_changed)
        self.file_card.files_changed.connect(self._sync_targets)
        self._engine_available = True
        self._wire_tasks()
        self._sync_targets()

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "target": self.cb_dst.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("target") in DST_VALUES:
            self.cb_dst.setCurrentText(prefs["target"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        ok, msg = convert_ebook(
            task.file_path, task.output_path, prog,
            cancel_check=lambda: task.state == tm.CANCELLED)
        if not ok:
            task.error = msg or tr("转换失败", "Convert failed")
            return False
        return True

    def _make_task(self, f):
        params = self.collect_params()
        if params.get("target") not in DST_VALUES:
            return None
        dst_ext = _DST_EXT.get(params.get("target", "EPUB"), ".epub")
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(f, out_dir, dst_ext)
        return dict(
            name=f"{tr('电子书转换', 'Ebook convert')} - {os.path.basename(f)}",
            task_type="ebook", file_path=f, output_path=out_path,
            params=params, runner=self._runner,
            history_type=tr("电子书互转", "Ebook convert"),
            history_target=params.get("target", "EPUB"),
            need_ffmpeg=False)

    def _start(self):
        # 执行前再次检查，避免页面显示就绪后 Calibre 被移动或卸载。
        self._on_target_changed()
        if self.file_card.files() and self._requires_calibre() and not self._engine_available:
            toast.show_warning(
                self,
                tr("该转换需要 Calibre。安装后点击“重新检测”。",
                   "This conversion requires Calibre. Install it, then click Recheck."))
            return False
        return self._submit_files()

    def _empty_hint(self):
        if self.file_card.files() and not self.cb_dst.isEnabled():
            return tr("当前批次没有可用的共同目标格式",
                      "This batch has no common target format")
        return tr("请先添加要转换的电子书文件", "Add ebook files first")

    def _sync_targets(self):
        """从整批源格式中排除同格式目标，避免提交后才报“无需转换”。"""
        files = self.file_card.files()
        previous = self.cb_dst.currentText()
        # .htm 与 .html 是同一源格式，不能因扩展名别名而显示同格式目标。
        source_exts = {os.path.splitext(path)[1].lower() for path in files}
        blocked = {SOURCE_EXT_ALIASES.get(ext, ext) for ext in source_exts}
        available = [name for name in DST_VALUES
                     if _DST_EXT[name] not in blocked]
        self.cb_dst.blockSignals(True)
        self.cb_dst.clear()
        if available:
            self.cb_dst.addItems(available)
            self.cb_dst.setCurrentText(
                previous if previous in available else available[0])
            self.cb_dst.setEnabled(True)
        else:
            self.cb_dst.addItem(tr("无可用目标", "No available target"))
            self.cb_dst.setEnabled(False)
        self.cb_dst.blockSignals(False)
        self._on_target_changed()

    def _requires_calibre(self) -> bool:
        return (self.cb_dst.currentText() in CALIBRE_TARGETS
                or any(os.path.splitext(path)[1].lower() in CALIBRE_SOURCE_EXTS
                       for path in self.file_card.files()))

    def _on_target_changed(self, *_args):
        target = self.cb_dst.currentText()
        self.file_card.set_target_fmt(target if target in DST_VALUES else "")
        requires_calibre = target in DST_VALUES and self._requires_calibre()
        self._engine_available = target in DST_VALUES and (
            not requires_calibre or bool(_find_calibre_convert()))
        self.engine_actions.setVisible(requires_calibre)
        if target not in DST_VALUES:
            self.engine_label.setText(tr("暂无可用转换", "No conversion available"))
            self.target_hint.setText(tr(
                "整批已包含全部目标格式，请按源格式分批转换。",
                "This batch includes every output format. Convert in batches grouped by source format."))
        else:
            self.target_hint.setText(TARGET_HINTS[target])
        if requires_calibre:
            self.engine_label.setText(
                tr("Calibre 已就绪", "Calibre ready")
                if self._engine_available else
                tr("未检测到 Calibre", "Calibre not detected"))
            self.engine_hint.setText(
                tr("使用本机 Calibre 处理，不上传文件。", "Uses local Calibre; files are not uploaded.")
                if self._engine_available else
                tr("该转换需要 Calibre；安装后可在此重新检测，无需重开页面。",
                   "Calibre is required. After installing it, recheck here without reopening this page."))
        elif target in DST_VALUES:
            self.engine_label.setText(tr("内置离线引擎", "Built-in offline engine"))
        self._sync_start_enabled()

    def _sync_start_enabled(self):
        TaskPanelMixin._sync_start_enabled(self)
        enabled = (self.action_bar.btn_go.isEnabled() and self.cb_dst.isEnabled()
                   and self._engine_available)
        self.action_bar.btn_go.setEnabled(enabled)
        if not self.cb_dst.isEnabled():
            self.action_bar.btn_go.setToolTip(self._empty_hint())
        elif self.file_card.files() and not self._engine_available:
            self.action_bar.btn_go.setToolTip(
                tr("请安装 Calibre 后点击“重新检测”", "Install Calibre, then click Recheck"))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "params_grid"):
            self.params_grid.set_columns(
                1 if self.viewport().width() < 820 else 2)
