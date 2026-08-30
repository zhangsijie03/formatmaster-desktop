# -*- coding: utf-8 -*-
"""batch_rename_panel — 批量重命名面板。

基于 core/tools.build_rename_plan（模板占位符 {n}/{name}/{ext}/{date}/
{time}/{folder} + 查找替换 + 正则替换 + 大小写转换）。
支持「重命名预览」：执行前先看新旧文件名对照。
即时执行（非队列任务），完成后刷新文件列表。
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QTableWidget,
                               QTableWidgetItem)
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, LineEdit,
                            MessageBox, PrimaryPushButton, PushButton)

from gui_qt.components import toast
from gui_qt.i18n import tr
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import FileListCard

CASE_VALUES = [tr("不转换", "Keep"), tr("大写", "UPPER"),
               tr("小写", "lower"), tr("首字母大写", "Title")]
CASE_KEYS = ("none", "upper", "lower", "title")


class BatchRenamePanelPage(BaseQtPanel):
    """批量重命名页。"""

    panel_key = "batch_rename"

    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("批量重命名", "Batch Rename"),
            tr("先预览完整方案，确认无冲突后再安全更新文件名",
               "Preview the complete plan, resolve conflicts, then rename safely"),
            FluentIcon.EDIT)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=None)
        self.file_card.table.setHorizontalHeaderLabels([
            tr("文件名", "Name"), tr("大小", "Size"),
            tr("扩展名", "Extension"), tr("状态", "Status")])
        lay.addWidget(self.file_card)

        lay.addWidget(self._build_params_card())

        preview_section = FormSection(tr("重命名预览", "Rename Preview"),
                                      FluentIcon.SEARCH)
        self.preview_status = CaptionLabel(
            tr("预览不会修改文件。添加文件并设置规则后，请先生成预览。",
               "Preview does not change files. Add files, set the rule, then generate a preview."))
        self.preview_status.setWordWrap(True)
        preview_section.add_widget(self.preview_status)
        self.preview_table = QTableWidget(0, 2, self)
        self.preview_table.setHorizontalHeaderLabels(
            [tr("原文件名", "Original"), tr("新文件名", "New")])
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.preview_table.setMaximumHeight(220)
        self.preview_table.setMinimumHeight(150)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setShowGrid(False)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview_table.setAccessibleName(
            tr("重命名方案预览", "Rename plan preview"))
        preview_section.add_widget(self.preview_table)
        lay.addWidget(preview_section)

        self.btn_preview = PushButton(
            FluentIcon.SEARCH, tr("生成预览", "Generate Preview"))
        self.btn_preview.clicked.connect(self._preview)
        self.btn_go = PrimaryPushButton(FluentIcon.EDIT, tr("开始重命名", "Rename"))
        self.btn_go.clicked.connect(self._run)
        self.btn_go.setEnabled(False)
        self.header.add_action(self.btn_preview)
        self.header.add_action(self.btn_go)
        # 标题组件在窄窗口构建期间会立即进入次行布局；第二个动态加入的
        # 按钮需要在本页重新同步一次，确保两个操作始终位于同一操作组。
        if self.header.width() < 820:
            self.header._actions_below = False
            self.header._sync_responsive_actions()

        self._preview_plan = None
        self.file_card.files_changed.connect(self._on_files_changed)
        for editor in (self.ed_pattern, self.ed_start, self.ed_search,
                       self.ed_replace, self.ed_regex, self.ed_regex_rep):
            editor.textChanged.connect(self._invalidate_preview)
        self.cb_case.currentIndexChanged.connect(self._invalidate_preview)

    def _build_params_card(self):
        sec = FormSection(tr("重命名规则", "Rename Rule"), FluentIcon.EDIT)
        self.rule_grid = FormGrid(columns=2)
        g = self.rule_grid
        self.placeholder_hint = CaptionLabel(
            tr("占位符：{n} 序号，{name} 原名，{ext} 扩展名，"
               "{date} 日期，{time} 时间，{folder} 文件夹",
               "Placeholders: {n} number, {name} original, {ext} extension, "
               "{date} date, {time} time, {folder} folder"))
        self.placeholder_hint.setWordWrap(True)
        sec.add_widget(self.placeholder_hint)
        self.rule_order_hint = CaptionLabel(tr(
            "执行顺序：命名模板，普通替换，正则替换，大小写转换。模板未包含 {ext} 时会自动保留原扩展名；查找和正则会作用于完整新文件名。",
            "Order: template, find and replace, regex, then case conversion. The original extension is kept when {ext} is omitted; find and regex apply to the complete new filename."))
        self.rule_order_hint.setWordWrap(True)
        sec.add_widget(self.rule_order_hint)
        self.ed_pattern = LineEdit()
        self.ed_pattern.setText(tr("文件_{n:03d}", "File_{n:03d}"))
        g.add_field(tr("命名模板", "Name template"), self.ed_pattern,
                    hint=tr("示例：照片_{n:03d} → 照片_001", "Example: Photo_{n:03d} → Photo_001"))
        self.ed_start = LineEdit()
        self.ed_start.setText("1")
        g.add_field(tr("开始序号", "Start number"), self.ed_start, hint="{n}")
        self.ed_search = LineEdit()
        self.ed_search.setPlaceholderText(tr("留空跳过…", "Leave blank to skip…"))
        g.add_field(tr("查找文本", "Find text"), self.ed_search)
        self.ed_replace = LineEdit()
        self.ed_replace.setPlaceholderText(tr("留空表示删除…", "Leave blank to delete…"))
        g.add_field(tr("替换为", "Replace with"), self.ed_replace)
        self.ed_regex = LineEdit()
        self.ed_regex.setPlaceholderText(tr(r"如 \s+ 或 ^IMG_…", r"e.g. \s+ or ^IMG_…"))
        g.add_field(tr("正则查找", "Regex find"), self.ed_regex,
                    hint=tr("按正则匹配替换（在查找替换之后执行）", "Applied after find/replace"))
        self.ed_regex_rep = LineEdit()
        self.ed_regex_rep.setPlaceholderText(tr("留空表示删除…", "Leave blank to delete…"))
        g.add_field(tr("正则替换为", "Regex replace"), self.ed_regex_rep)
        self.cb_case = ComboBox()
        self.cb_case.addItems(CASE_VALUES)
        g.add_field(tr("大小写", "Case"), self.cb_case)
        sec.add_form(g)
        return sec

    def _rule_args(self):
        """收集当前规则参数（不含文件列表）。"""
        try:
            start = int(self.ed_start.text().strip())
        except ValueError as exc:
            raise ValueError(tr("开始序号必须是整数",
                                "Start number must be an integer")) from exc
        if start < 0:
            raise ValueError(tr("开始序号不能小于 0",
                                "Start number cannot be negative"))
        case_index = self.cb_case.currentIndex()
        return dict(
            start_num=start,
            search_text=self.ed_search.text(),
            replace_text=self.ed_replace.text(),
            case=(CASE_KEYS[case_index]
                  if 0 <= case_index < len(CASE_KEYS) else "none"),
            regex_pattern=self.ed_regex.text(),
            regex_replace=self.ed_regex_rep.text())

    def _plan(self):
        """计算重命名方案；返回 (plan, None) 或 (None, 错误消息)。"""
        files = self.file_card.files()
        if not files:
            return None, tr("请先添加要重命名的文件", "Add files first")
        pattern = self.ed_pattern.text().strip()
        if not pattern:
            return None, tr("请填写命名模板", "Fill in the name template")
        from core.tools import build_rename_plan
        try:
            plan = build_rename_plan(files, pattern, **self._rule_args())
        except (OSError, ValueError) as exc:
            return None, tr("规则错误：{}", "Rule error: {}").format(exc)
        return plan, None

    def _on_files_changed(self):
        """将通用转换列调整为重命名页面语义。"""
        for row, path in enumerate(self.file_card.files()):
            extension = os.path.splitext(path)[1].lstrip(".").upper() or "--"
            self.file_card.table.item(row, 2).setText(extension)
            self.file_card.table.item(row, 3).setText(
                tr("待预览", "Needs preview"))
        self._invalidate_preview()

    def _invalidate_preview(self):
        """规则或文件集变化后禁止执行旧预览。"""
        if not hasattr(self, "preview_table"):
            return
        self._preview_plan = None
        self.preview_table.setRowCount(0)
        self.preview_status.setText(
            tr("规则或文件已变更，旧预览已失效，请重新生成。",
               "Rules or files changed. The old preview is no longer valid; generate it again."))
        for row in range(self.file_card.table.rowCount()):
            state_item = self.file_card.table.item(row, 3)
            if state_item is not None:
                state_item.setText(tr("待预览", "Needs preview"))
        self.btn_go.setEnabled(False)

    def _preview(self):
        plan, err = self._plan()
        if err:
            toast.show_warning(self, err)
            return
        self.preview_table.setRowCount(0)
        changed = 0
        for i, (src, new_name, _new_path) in enumerate(plan):
            old = os.path.basename(src)
            if new_name != old:
                changed += 1
            self.preview_table.insertRow(i)
            old_item = QTableWidgetItem(old)
            old_item.setToolTip(src)
            new_item = QTableWidgetItem(new_name)
            new_item.setToolTip(_new_path)
            self.preview_table.setItem(i, 0, old_item)
            self.preview_table.setItem(i, 1, new_item)
            state_item = self.file_card.table.item(i, 3)
            if state_item is not None:
                state_item.setText(
                    tr("将重命名", "Will rename")
                    if new_name != old else tr("保持不变", "Unchanged"))
        self._preview_plan = list(plan)
        self.btn_go.setEnabled(changed > 0)
        if changed:
            status = tr("已校验：{} 个文件将重命名，{} 个保持不变。执行会直接更新源文件名。",
                        "Validated: {} files will be renamed and {} unchanged. Running the plan directly updates source filenames.").format(
                            changed, len(plan) - changed)
        else:
            status = tr("所有文件名均不变，无需执行",
                        "All names are unchanged; nothing to rename")
        self.preview_status.setText(status)
        toast.show_info(
            self, tr("预览：{} 个文件将改名（共 {} 个）",
                     "Preview: {} will be renamed (of {})")
            .format(changed, len(plan)))

    def _confirm_run(self, count):
        """重命名直接修改源文件，执行前要求明确确认。"""
        box = MessageBox(
            tr("确认批量重命名？", "Confirm batch rename?"),
            tr("将按预览方案更新 {} 个源文件的名称。文件内容不会改变；执行失败时会尝试恢复原文件名。",
               "Rename {} source files exactly as previewed. File contents stay unchanged, and a failed run attempts to restore the original names.").format(count),
            self)
        box.yesButton.setText(tr("确认重命名", "Rename Files"))
        box.cancelButton.setText(tr("取消", "Cancel"))
        confirmed = [False]
        box.yesButton.clicked.connect(
            lambda: confirmed.__setitem__(0, True))
        box.exec()
        return confirmed[0]

    def _run(self):
        if not self._preview_plan:
            toast.show_warning(
                self, tr("请先生成有效预览",
                         "Generate a valid preview first"))
            return
        changed_count = sum(
            os.path.abspath(src) != os.path.abspath(target)
            for src, _name, target in self._preview_plan)
        if not changed_count or not self._confirm_run(changed_count):
            return

        from core.tools import execute_rename_plan
        plan = list(self._preview_plan)
        self.btn_go.setEnabled(False)
        self.btn_preview.setEnabled(False)
        try:
            renamed = execute_rename_plan(plan)
        except OSError as exc:
            toast.show_error(
                self, tr("重命名失败：{}",
                         "Rename failed: {}").format(exc))
            self.btn_preview.setEnabled(True)
            self._invalidate_preview()
            return

        final_paths = [target if os.path.abspath(src) != os.path.abspath(target)
                       else src for src, _name, target in plan]
        self.file_card.clear_files()
        self.file_card.add_files(final_paths)
        self.btn_preview.setEnabled(True)
        toast.show_success(
            self, tr("已安全重命名 {} 个文件",
                     "Safely renamed {} files").format(len(renamed)))

    def collect_prefs(self) -> dict:
        """记忆命名模板/序号/查找替换/正则/大小写，重进面板自动恢复。"""
        return {"pattern": self.ed_pattern.text(),
                "start": self.ed_start.text(),
                "search": self.ed_search.text(),
                "replace": self.ed_replace.text(),
                "regex": self.ed_regex.text(),
                "regex_rep": self.ed_regex_rep.text(),
                "case": CASE_KEYS[self.cb_case.currentIndex()]}

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        for k in ("pattern", "start", "search", "replace", "regex", "regex_rep"):
            v = prefs.get(k)
            if v is not None:
                getattr(self, f"ed_{k}").setText(v)
        ci = prefs.get("case")
        if isinstance(ci, str) and ci in CASE_KEYS:
            self.cb_case.setCurrentIndex(CASE_KEYS.index(ci))
        elif isinstance(ci, int) and 0 <= ci < len(CASE_VALUES):
            # 兼容旧版按下标保存的偏好。
            self.cb_case.setCurrentIndex(ci)

    def resizeEvent(self, event):
        """窄窗口下规则改为单列，避免输入框被挤压或水平溢出。"""
        super().resizeEvent(event)
        if hasattr(self, "rule_grid"):
            narrow = self.width() < 760
            self.rule_grid.set_columns(1 if narrow else 2)
            if hasattr(self, "btn_preview"):
                self.btn_preview.setText(
                    tr("预览", "Preview") if narrow
                    else tr("生成预览", "Generate Preview"))
                self.btn_go.setText(
                    tr("重命名", "Rename") if narrow
                    else tr("开始重命名", "Rename"))
                # 仅缩短本页文件列表按钮文案，释放窄屏表格的最小宽度。
                self.file_card.btn_add.setText(
                    tr("添加", "Add") if narrow else tr("添加文件", "Add files"))
                self.file_card.btn_add_dir.setText(
                    tr("文件夹", "Folder") if narrow else tr("添加文件夹", "Add folder"))
                self.file_card.btn_rm.setText(
                    tr("移除", "Remove") if narrow else tr("移除选中", "Remove"))
