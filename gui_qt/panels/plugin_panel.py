"""plugin_panel — 插件系统管理面板（卡片网格布局）。

扫描 plugins/ 目录（用户数据目录 + 项目目录），以「图标 + 标题 + 简短描述」
的卡片形式排列；点击选中并直接打开面板；支持重新扫描与导入插件
（.py / 压缩包 / 文件夹，成功/失败均有弹窗提示，失败不落地）。
"""

import os
from enum import Enum

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (QBoxLayout, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QMenu, QSizePolicy,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, IconWidget,
                            MessageDialog, PushButton, SearchLineEdit,
                            StrongBodyLabel, ToolButton)

from gui_qt.components import toast
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from plugins._i18n import t

# 插件名关键词 → 图标（全部使用 FluentIcon 内置图标：风格统一、零额外依赖、
# 不会出现因第三方字体库缺失而整排回退成同一个机器人的情况）。
# 规则按「具体优先」排列：先匹配到的关键词即生效，靠后的通用词不抢占
# （如「编码」放在「Unicode」之后，避免误命中「Unicode 编解码」）。
_ICON_RULES = [
    ("查找替换", FluentIcon.SEARCH),
    ("JSON", FluentIcon.CODE),
    ("时间戳", FluentIcon.GLOBE),
    ("Base64", FluentIcon.CERTIFICATE),
    ("正则", FluentIcon.FILTER),
    ("UUID", FluentIcon.DEVELOPER_TOOLS),
    ("Markdown", FluentIcon.DOCUMENT),
    ("字幕", FluentIcon.VIDEO),
    ("反转", FluentIcon.SYNC),
    ("哈希", FluentIcon.FINGERPRINT),
    ("身份证", FluentIcon.PEOPLE),
    ("IP", FluentIcon.CONNECT),
    ("摩斯", FluentIcon.MESSAGE),
    ("九宫格", FluentIcon.TILES),
    ("颜色", FluentIcon.PALETTE),
    ("进制", FluentIcon.UNIT),
    ("对比", FluentIcon.HIGHTLIGHT),
    ("去重", FluentIcon.MOVE),
    ("数字", FluentIcon.LABEL),
    ("ASCII", FluentIcon.PENCIL_INK),
    ("字数", FluentIcon.DICTIONARY),
    ("简繁", FluentIcon.LANGUAGE),
    ("HTML", FluentIcon.LAYOUT),
    ("CSV", FluentIcon.ALIGNMENT),
    ("SQL", FluentIcon.CODE),
    ("图片", FluentIcon.PHOTO),
    ("URL", FluentIcon.LINK),
    ("网页", FluentIcon.PRINT),
    ("JWT", FluentIcon.TAG),
    ("Unicode", FluentIcon.EMOJI_TAB_SYMBOLS),
    ("编码", FluentIcon.FONT),
]
_DEFAULT_ICON = FluentIcon.ROBOT


class PluginSource(str, Enum):
    ALL = "all"
    BUILTIN = "builtin"
    IMPORTED = "imported"


def _icon_for(name):
    """插件图标：按名称关键词映射 FluentIcon，未命中则回退 ROBOT。"""
    for kw, icon in _ICON_RULES:
        if kw in name:
            return icon
    return _DEFAULT_ICON


class _PluginCard(QFrame):
    """插件卡片：图标 + 标题 + 简短描述，点击选中，右键菜单。"""

    clicked = Signal(int)
    delete_requested = Signal(int)
    edit_requested = Signal(int)

    def __init__(self, idx, name, desc, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.setObjectName("pluginCard")
        self.setAttribute(Qt.WA_Hover, True)
        self.setMinimumWidth(156)
        self.setFixedHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(t(name))
        self.setAccessibleDescription(t(desc or ""))
        self.setToolTip(t(desc or name))

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(6)
        # 图标匹配用中文原名（_ICON_RULES 为中文关键词），显示文本走翻译
        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.setSpacing(6)
        self.ic = IconWidget(_icon_for(name), self)
        self.ic.setFixedSize(30, 30)
        icon_row.addWidget(self.ic)
        icon_row.addStretch(1)
        self.lb_source = CaptionLabel(self)
        icon_row.addWidget(self.lb_source)
        self.btn_menu = ToolButton(FluentIcon.MORE, self)
        self.btn_menu.setFixedSize(28, 28)
        self.btn_menu.setToolTip(tr("插件操作", "Plugin actions"))
        self.btn_menu.setAccessibleName(tr("插件操作", "Plugin actions"))
        self.btn_menu.clicked.connect(self._show_button_menu)
        icon_row.addWidget(self.btn_menu)
        v.addLayout(icon_row)
        self.lb_name = StrongBodyLabel(t(name), self)
        self.lb_name.setTextFormat(Qt.PlainText)
        self.lb_name.setMinimumWidth(0)
        self._full_name = t(name)
        self.lb_name.setToolTip(self._full_name)
        self.lb_name.setStyleSheet("font-size: 14px;")
        v.addWidget(self.lb_name)
        self.lb_desc = CaptionLabel(t(desc or ""), self)
        self.lb_desc.setTextFormat(Qt.PlainText)
        self.lb_desc.setWordWrap(True)
        self.lb_desc.setStyleSheet("font-size: 12px;")
        # 描述最多两行（40px ≈ 2×行高）：更长的描述在此截断，防止文字溢出卡片
        # 下边框（长描述 + 用户 DPI/字体偏大时原布局会把文字顶出卡片）。
        self.lb_desc.setMaximumHeight(40)
        v.addWidget(self.lb_desc)
        v.addStretch(1)

        # 右键菜单：删除插件（左键仍为打开面板）
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_manageable(self, enabled):
        """内置插件没有可执行管理动作，不显示误导性的菜单入口。"""
        self.btn_menu.setVisible(enabled)
        self.source_kind = PluginSource.IMPORTED if enabled else PluginSource.BUILTIN
        self.lb_source.setText(tr("已导入", "Imported") if enabled
                               else tr("内置", "Built-in"))
        self.setContextMenuPolicy(
            Qt.CustomContextMenu if enabled else Qt.NoContextMenu)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 插件名来自外部元数据；长名称不挤破网格，完整名称保留在悬停提示中。
        self.lb_name.setText(self.lb_name.fontMetrics().elidedText(
            self._full_name, Qt.ElideRight, max(1, self.width() - 28)))

    def _on_context_menu(self, pos):
        self._show_menu(self.mapToGlobal(pos))

    def _show_button_menu(self):
        """显式菜单替代只能靠猜测的右键入口。"""
        self._show_menu(self.btn_menu.mapToGlobal(
            QPoint(0, self.btn_menu.height())))

    def _show_menu(self, global_pos):
        from gui_qt.i18n import tr
        menu = QMenu(self)
        act_edit = menu.addAction(
            FluentIcon.CODE.icon(), tr("编辑源码", "Edit source"))
        act_del = menu.addAction(
            FluentIcon.DELETE.icon(), tr("删除插件", "Delete plugin"))
        act = menu.exec(global_pos)
        if act == act_edit:
            self.edit_requested.emit(self.idx)
        elif act == act_del:
            self.delete_requested.emit(self.idx)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.idx)
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, event):
        """卡片与按钮行为一致：Enter/Space 均可打开插件。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.idx)
            event.accept()
            return
        super().keyPressEvent(event)


class PluginPanelPage(BaseQtPanel):
    """插件管理页（卡片网格）。"""

    panel_key = "plugins"

    def build(self):
        lay = self.content_layout
        lay.addWidget(self.make_title(tr("插件中心", "Plugins")))
        lay.addWidget(CaptionLabel(
            tr("扫描 plugins 目录加载扩展插件（Python 文件，支持自定义面板）",
               "Load Python plugins from the plugins folder")))

        self.bar = QBoxLayout(QBoxLayout.LeftToRight)
        self.bar.setSpacing(8)
        action_wrap = QWidget(self)
        action_row = QHBoxLayout(action_wrap)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self.btn_scan = PushButton(FluentIcon.SYNC, tr("重新扫描", "Rescan"))
        self.btn_scan.clicked.connect(self._scan)
        action_row.addWidget(self.btn_scan)
        self.btn_import = PushButton(FluentIcon.DOWNLOAD,
                                     tr("导入插件", "Import"))
        self.btn_import.clicked.connect(self._import_menu)
        action_row.addWidget(self.btn_import)
        action_row.addStretch(1)
        self.bar.addWidget(action_wrap)
        filter_wrap = QWidget(self)
        filter_row = QHBoxLayout(filter_wrap)
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.source_filter = ComboBox(self)
        for key, label in ((PluginSource.ALL, tr("全部来源", "All sources")),
                           (PluginSource.BUILTIN, tr("内置插件", "Built-in")),
                           (PluginSource.IMPORTED, tr("已导入插件", "Imported"))):
            self.source_filter.addItem(label, userData=key)
        self.source_filter.setAccessibleName(tr("插件来源", "Plugin source"))
        self.source_filter.currentIndexChanged.connect(
            lambda _index: self._apply_filter(self.search_edit.text()))
        filter_row.addWidget(self.source_filter)
        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(
            tr("搜索插件…", "Search plugins…"))
        self.search_edit.setAccessibleName(tr("搜索插件", "Search plugins"))
        self.search_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_edit.setMaximumWidth(320)
        self.search_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.search_edit, 1)
        self.bar.addWidget(filter_wrap, 1)
        self.lb_path = CaptionLabel("")
        self.lb_path.setStyleSheet(
            f"font-size: 12px; color: {self._ink_sec()};")
        self.bar.addStretch(1)
        self.bar.addWidget(self.lb_path)
        lay.addLayout(self.bar)

        # 导入插件注意事项（常驻提示）
        self.lb_tip = CaptionLabel(
            tr("点击卡片打开工具。支持导入 .py 文件、ZIP 压缩包或文件夹；插件会执行代码，请仅导入可信来源。",
               "Select a card to open a tool. Import .py files, ZIP archives or folders only from trusted sources: plugins execute code."))
        self.lb_tip.setToolTip(tr(
            "插件须包含 PLUGIN_INFO（name 必填）；可参考项目自带的 UUID 生成器。导入失败会说明原因并回滚文件。",
            "Plugins require PLUGIN_INFO with a name. Use the built-in UUID generator as a template. Failed imports report an error and roll back files."))
        self.lb_tip.setWordWrap(True)
        self.lb_tip.setStyleSheet(
            f"font-size: 12px; color: {self._ink_sec()};")
        lay.addWidget(self.lb_tip)

        self.btn_errors = PushButton(FluentIcon.INFO, "", self)
        self.btn_errors.clicked.connect(self._show_load_errors)
        self.btn_errors.hide()
        lay.addWidget(self.btn_errors, 0, Qt.AlignLeft)

        # 卡片网格按可用宽度重排。固定每张卡片宽度的 FlowLayout
        # 会把内容区撑宽并产生水平滚动，网格可让卡片共享剩余宽度。
        self.cards_wrap = QWidget()
        self.cards_grid = QGridLayout(self.cards_wrap)
        self.cards_grid.setContentsMargins(0, 6, 0, 6)
        self.cards_grid.setHorizontalSpacing(10)
        self.cards_grid.setVerticalSpacing(10)
        lay.addWidget(self.cards_wrap)
        self.lb_empty = CaptionLabel("")
        self.lb_empty.setAlignment(Qt.AlignCenter)
        self.lb_empty.setMinimumHeight(88)
        self.lb_empty.hide()
        lay.addWidget(self.lb_empty)
        self.btn_clear_filter = PushButton(tr("清除筛选", "Clear filters"), self)
        self.btn_clear_filter.clicked.connect(self._clear_filter)
        self.btn_clear_filter.hide()
        lay.addWidget(self.btn_clear_filter, 0, Qt.AlignHCenter)

        self._plugins = []
        self._cards = []
        self._selected = -1
        self._load_errors = []
        self._errors_dialog = None
        self._scanning = False
        # 插件工具窗必须由页面持有；open() 返回后若没有强引用，Python 会立即
        # 回收窗口。这里同时用于阻止重复打开同一个模态窗。
        self._panel_dialog = None
        self._apply_theme()
        from gui_qt.components import design_system as ds
        ds.bind_theme(self, self._apply_theme)
        self._scan()

    def _apply_theme(self):
        """卡片选中/悬停样式按主题刷新。"""
        from gui_qt.components import design_system as ds
        t = ds.tokens()
        for label in (self.lb_tip, self.lb_path):
            label.setStyleSheet(f"font-size: 12px; color: {t['ink_sec']};")
        qss = (
            f"QFrame#pluginCard {{ background: {t['card_bg']};"
            f" border: 1px solid {t['border']}; border-radius: 10px; }}"
            f"QFrame#pluginCard:hover {{ background: {t['card_hover']};"
            f" border-color: {t['border_hi']}; }}"
            f"QFrame#pluginCard:focus {{ border: 2px solid {t['accent']}; }}"
            f"QFrame#pluginCard[selected=\"true\"] {{"
            f" border: 2px solid {t['accent']};"
            f" background: {t['accent_pale']}; }}"
            f"QFrame#pluginCard QLabel {{ background: transparent;"
            f" color: {t['ink']}; border: none; }}"
            f"QFrame#pluginCard CaptionLabel {{ color: {t['ink_sec']}; }}"
        )
        self._card_qss = qss
        for c in self._cards:
            c.setStyleSheet(qss)

    def _ink_sec(self):
        from gui_qt.components import design_system as ds
        return ds.ink_sec()

    def _clear_cards(self):
        for c in self._cards:
            self.cards_grid.removeWidget(c)
            c.deleteLater()
        self._cards = []
        self._selected = -1

    def _scan(self):
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan.setEnabled(False)
        self.btn_import.setEnabled(False)
        try:
            self._scan_plugins()
        except Exception as error:  # 扫描失败保留现有页面，并恢复用户重试入口。
            toast.show_error(self, tr("扫描失败：{}", "Scan failed: {}").format(error))
        finally:
            self._scanning = False
            self.btn_scan.setEnabled(True)
            self.btn_import.setEnabled(True)

    def _scan_plugins(self):
        from core.plugin_loader import plugin_dirs, scan_plugins
        self._plugins, load_errors = scan_plugins(include_errors=True)
        self._load_errors = load_errors
        self._clear_cards()
        for i, p in enumerate(self._plugins):
            card = _PluginCard(i, p.name, p.description or "", self.cards_wrap)
            card.clicked.connect(self._on_card_clicked)
            card.delete_requested.connect(self._delete_plugin)
            card.edit_requested.connect(self._edit_plugin)
            card.set_manageable(self._is_user_plugin(p.source))
            card.setStyleSheet(self._card_qss)
            self._cards.append(card)
        self._apply_filter(self.search_edit.text())
        paths = os.pathsep.join(plugin_dirs())
        self.btn_errors.setVisible(bool(load_errors))
        self.btn_errors.setText(tr("查看 {} 项加载失败", "View {} load failures").format(len(load_errors)))
        if load_errors:
            self.lb_path.setToolTip(
                paths + "\n" + tr("加载失败：", "Failed: ")
                + ", ".join(load_errors))
        else:
            self.lb_path.setToolTip(paths)
        if not self._plugins:
            toast.show_info(self, tr("未发现插件（检查 plugins 目录）",
                                     "No plugins found (check plugins dir)"))

    def _apply_filter(self, text=""):
        """按名称和说明过滤插件，避免几十张卡片形成不可扫描的信息墙。"""
        keyword = (text or "").strip().casefold()
        source = self.source_filter.currentData()
        shown = 0
        for card, plugin in zip(self._cards, self._plugins):
            # 同时匹配原名、翻译与稳定文件名，中英文切换后仍能找到同一工具。
            haystack = (f"{plugin.name} {plugin.description or ''} "
                        f"{t(plugin.name)} {t(plugin.description or '')} "
                        f"{os.path.basename(plugin.source)}").casefold()
            matches = ((not keyword or keyword in haystack)
                       and (source == PluginSource.ALL or source == card.source_kind))
            card.setVisible(matches)
            shown += int(matches)
        filtered = bool(keyword) or source != PluginSource.ALL
        self.lb_path.setText(
            tr("显示 {} / {} 个插件", "Showing {} / {} plugins").format(shown, len(self._plugins))
            if filtered else tr("共 {} 个插件", "{} plugins").format(len(self._plugins)))
        self.btn_clear_filter.setVisible(filtered)
        self._relayout_cards()
        self._update_empty_state(keyword)

    def _update_empty_state(self, keyword=""):
        """空目录与无搜索结果都给出可见说明，避免只剩空白区域。"""
        visible = any(not card.isHidden() for card in self._cards)
        self.cards_wrap.setVisible(visible)
        self.lb_empty.setText(
            tr("没有匹配的插件，请调整搜索条件。",
               "No matching plugins. Try another search.")
            if keyword or self.source_filter.currentData() != PluginSource.ALL else
            tr("未发现插件，可导入可信来源的插件。",
               "No plugins found. Import a plugin from a trusted source."))
        self.lb_empty.setVisible(not visible)

    def _clear_filter(self):
        self.source_filter.setCurrentIndex(0)
        self.search_edit.clear()
        self._apply_filter()

    def _show_load_errors(self):
        """失败文件名以纯文本、可滚动方式展示，不把外部名称解释成富文本。"""
        if not self._load_errors:
            return
        if self._errors_dialog is not None:
            self._errors_dialog.raise_()
            self._errors_dialog.activateWindow()
            return
        from qfluentwidgets import TextEdit
        from gui_qt.components.dialog import FluentDialogBase
        from gui_qt.components import design_system as ds
        dialog = FluentDialogBase(tr("插件加载失败", "Plugin load failures"), self)
        dialog.resize(*ds.screen_ratio_size(0.7, max_w=580, max_h=420))
        layout = QVBoxLayout(dialog)
        hint = CaptionLabel(tr("以下文件未能加载，请检查依赖或源码后重新扫描。",
                               "These files could not load. Check dependencies or source, then rescan."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        text = TextEdit(dialog)
        text.setReadOnly(True)
        text.setAccessibleName(tr("加载失败文件", "Files that failed to load"))
        text.setPlainText("\n".join(self._load_errors))
        layout.addWidget(text, 1)
        close = PushButton(tr("关闭", "Close"), dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignRight)
        self._errors_dialog = dialog

        def release(_result):
            self._errors_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(release)
        dialog.open()

    def _relayout_cards(self):
        """按页面可用宽度分配 1–4 列，保证说明文字保有可读宽度。"""
        width = max(1, self.width() - 50)
        columns = max(1, min(4, width // 240))
        visible_cards = [card for card in self._cards if not card.isHidden()]
        for card in self._cards:
            self.cards_grid.removeWidget(card)
        for column in range(5):
            self.cards_grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, card in enumerate(visible_cards):
            self.cards_grid.addWidget(card, index // columns, index % columns)
        rows = ((len(visible_cards) + columns - 1) // columns
                if visible_cards else 0)
        self.cards_wrap.setFixedHeight(
            rows * 112 + max(0, rows - 1) * 10 + (12 if rows else 0))
        self.cards_grid.invalidate()
        self.cards_wrap.updateGeometry()
        self.content_layout.invalidate()
        self.content.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 1000
        self.bar.setDirection(QBoxLayout.TopToBottom if narrow
                              else QBoxLayout.LeftToRight)
        self.search_edit.setMaximumWidth(16777215 if narrow else 320)
        self._relayout_cards()

    def _on_card_clicked(self, idx):
        """点击卡片：选中高亮 + 直接打开插件面板。"""
        self._select(idx)
        self._open_panel(idx)

    def _edit_plugin(self, idx):
        """右键「编辑源码」：QPlainTextEdit+语法高亮查看/编辑插件源码。"""
        if not (0 <= idx < len(self._plugins)):
            return
        p = self._plugins[idx]
        if not self._is_user_plugin(p.source):
            toast.show_info(
                self, tr("内置插件仅供使用，不能直接修改",
                         "Built-in plugins cannot be edited"))
            return
        if not os.path.isfile(p.source):
            toast.show_warning(self, tr("插件源码不存在", "Plugin source missing"))
            return
        from gui_qt.components.code_editor import CodeEditorDialog
        dlg = CodeEditorDialog(p.source, self.window())
        dlg.exec()
        # 编辑后重新扫描，让改动立即生效
        self._scan()

    def _delete_plugin(self, idx):
        """右键删除插件：仅用户导入的插件可删（内置插件受保护）。"""
        if not (0 <= idx < len(self._plugins)):
            return
        p = self._plugins[idx]
        if not self._is_user_plugin(p.source):
            toast.show_info(
                self, tr("内置插件不可删除", "Built-in plugins cannot be deleted"))
            return

        # 确认删除
        try:
            from qfluentwidgets import MessageBox
            box = MessageBox(
                tr("删除插件", "Delete plugin"),
                tr("确定删除插件「{}」吗？删除后不可恢复。",
                   "Delete plugin \"{}\"? This cannot be undone.").format(t(p.name)),
                self)
            box.yesButton.setText(tr("删除", "Delete"))
            box.cancelButton.setText(tr("取消", "Cancel"))
            if not box.exec():
                return
        except Exception as exc:  # noqa: BLE001 - 无法确认时必须保持文件不变
            toast.show_error(
                self, tr("无法显示删除确认框：{}",
                         "Could not confirm deletion: {}").format(exc))
            return

        try:
            os.remove(p.source)
        except OSError as e:
            toast.show_error(self, tr("删除失败：{}", "Delete failed: {}").format(e))
            return
        self._scan()
        toast.show_success(self, tr("已删除插件：{}", "Plugin deleted: {}").format(t(p.name)))

    @staticmethod
    def _is_user_plugin(source):
        """仅真实位于用户插件目录的普通文件允许编辑或删除。"""
        from core.plugin_loader import plugin_dirs
        dirs = plugin_dirs()
        if not dirs:
            return False
        user_dir = os.path.realpath(dirs[0])
        src = os.path.realpath(os.path.abspath(source))
        try:
            return (os.path.commonpath([src, user_dir]) == user_dir
                    and os.path.isfile(src) and not os.path.islink(source))
        except ValueError:  # 不同盘符无公共路径
            return False

    # ── 导入插件 ────────────────────────────────
    def _import_menu(self):
        """导入按钮菜单：文件/压缩包 or 文件夹。"""
        menu = QMenu(self)
        act_file = menu.addAction(
            FluentIcon.DOCUMENT.icon(), tr("从文件 / 压缩包导入",
                                           "From file / zip"))
        act_dir = menu.addAction(
            FluentIcon.FOLDER.icon(), tr("从文件夹导入", "From folder"))
        act = menu.exec(self.btn_import.mapToGlobal(
            QPoint(0, self.btn_import.height())))
        if act == act_file:
            self._import_file()
        elif act == act_dir:
            self._import_dir()

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择插件文件或压缩包", "Pick plugin file or zip"),
            "", tr("插件文件 (*.py *.zip);;Python (*.py);;压缩包 (*.zip)",
                   "Plugins (*.py *.zip);;Python (*.py);;Zip (*.zip)"))
        if path:
            self._do_import(path)

    def _import_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, tr("选择包含插件的文件夹", "Pick plugin folder"))
        if path:
            self._do_import(path)

    def _do_import(self, source):
        """执行导入：全部有效才落地，结果弹窗提示，成功则重新扫描。"""
        from qfluentwidgets import MessageBox
        try:
            box = MessageBox(
                tr("确认导入插件", "Confirm plugin import"),
                tr("插件是可执行 Python 代码，将拥有与本应用相同的文件访问权限。"
                   "请仅继续导入你信任且已审查的来源。",
                   "Plugins are executable Python code with the same file access "
                   "as this app. Continue only if you trust and reviewed the source."),
                self)
            box.yesButton.setText(tr("继续导入", "Import"))
            box.cancelButton.setText(tr("取消", "Cancel"))
            if not box.exec():
                return
        except Exception as exc:  # noqa: BLE001 - 无法确认时不得执行插件代码
            toast.show_error(
                self, tr("无法确认插件导入：{}",
                         "Could not confirm import: {}").format(exc))
            return
        from core.plugin_loader import import_plugin, plugin_dirs
        target = plugin_dirs()[0]   # 用户数据目录，导入即用户私有
        ok, result = import_plugin(source, target)
        if ok:
            self._scan()
        self._show_import_result(ok, result)

    def _show_import_result(self, ok, result):
        """导入成功/失败弹窗。"""
        if ok:
            names = tr("、", ", ").join(t(str(item)) for item in result) \
                if isinstance(result, list) else t(str(result))
            dlg = MessageDialog(
                tr("导入成功", "Imported"),
                tr("已导入插件：{}", "Plugin imported: {}").format(names),
                self)
        else:
            dlg = MessageDialog(tr("导入失败", "Import failed"),
                                str(result), self)
        dlg.yesButton.setText(tr("知道了", "OK"))
        dlg.cancelButton.hide()
        dlg.exec()

    def _select(self, idx):
        """单选高亮。"""
        if not (0 <= idx < len(self._cards)):
            return
        self._selected = idx
        for i, c in enumerate(self._cards):
            sel = (i == idx)
            if c.property("selected") != sel:
                c.setProperty("selected", sel)
                c.style().unpolish(c)
                c.style().polish(c)

    def _open_panel(self, idx, dialog_parent=None):
        if not (0 <= idx < len(self._plugins)):
            return
        if self._panel_dialog is not None:
            # 已打开的插件窗保持单实例，避免叠出多个窗口及多层模态事件循环。
            self._panel_dialog.raise_()
            self._panel_dialog.activateWindow()
            return
        p = self._plugins[idx]
        if p.panel_class is None:
            toast.show_info(self, tr("该插件没有界面", "This plugin has no panel"))
            return
        try:
            # FluentDialogBase：自动适配亮/暗主题并跟随切换
            from gui_qt.components import design_system as ds
            from gui_qt.components.dialog import FluentDialogBase
            owner = dialog_parent or self
            dlg = FluentDialogBase(
                f"{t(p.name)} · {tr('插件面板', 'Plugin panel')}", owner)
            w, h = ds.screen_ratio_size(0.82)
            dlg.resize(w, h)
            from PySide6.QtWidgets import QVBoxLayout
            v = QVBoxLayout(dlg)
            v.addWidget(p.panel_class())
            self._panel_dialog = dlg

            def _release_dialog(_result):
                if self._panel_dialog is dlg:
                    self._panel_dialog = None
                dlg.deleteLater()

            dlg.finished.connect(_release_dialog)
            # macOS 下 exec() 会创建第二层 Qt 事件循环。点击原生交通灯时，
            # Cocoa 关闭窗口后 Qt 还要退出嵌套循环并恢复父窗焦点，表现为一次
            # 额外闪烁；open() 保留窗口模态语义，但只使用主事件循环。
            dlg.open()
        except Exception as e:  # noqa: BLE001
            self._panel_dialog = None
            toast.show_error(self, f"{tr('插件面板打开失败', 'Open failed')}: {e}")

    def open_plugin(self, plugin_id, dialog_parent=None):
        """按稳定文件标识打开插件，供首页快捷入口复用。"""
        normalized = str(plugin_id or "").strip()
        for index, plugin in enumerate(self._plugins):
            source_id = os.path.splitext(os.path.basename(plugin.source))[0]
            if source_id == normalized:
                self._select(index)
                self._open_panel(index, dialog_parent)
                return True
        toast.show_warning(
            self,
            tr("插件已不可用，请重新自定义首页快捷功能",
               "Plugin unavailable; update your home shortcuts"))
        return False
