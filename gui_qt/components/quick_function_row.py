"""首页快捷入口：单行展示，并允许用户选择功能或可视化插件。"""

import os

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QListWidget,
                               QListWidgetItem, QDialog, QSizePolicy,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, FluentIcon, IconWidget,
                            PrimaryPushButton, PushButton, SearchLineEdit,
                            StrongBodyLabel)

from gui_qt.components import design_system as ds
from gui_qt.components.card import Card
from gui_qt.components.dialog import FluentDialogBase
from gui_qt.i18n import tr

MAX_SHORTCUTS = 6
PREF_KEY = "home_quick_functions"
DEFAULT_SHORTCUTS = [
    "video", "audio", "image", "document", "video_compress", "gif",
]

def _shortcut_candidates():
    """从导航注册表和插件扫描结果生成稳定、可持久化的快捷入口。"""
    from gui_qt.nav_registry import all_items, label

    excluded = {"home", "history", "settings", "plugins"}
    candidates = []
    for item in all_items():
        if item["key"] in excluded:
            continue
        candidates.append({
            "id": item["key"],
            "title": label(item),
            "icon": item["icon"],
            "accent": ds.accent(),
            "kind": tr("功能", "Feature"),
        })

    # 插件扫描失败不能阻断首页；错误仍会在插件中心的显式扫描中反馈。
    try:
        from core.plugin_loader import scan_plugins
        from gui_qt.panels.plugin_panel import _icon_for
        from plugins._i18n import t as plugin_text

        seen = set()
        for plugin in scan_plugins():
            plugin_id = os.path.splitext(os.path.basename(plugin.source))[0]
            if plugin.panel_class is None or plugin_id in seen:
                continue
            seen.add(plugin_id)
            candidates.append({
                "id": f"plugin:{plugin_id}",
                "title": plugin_text(plugin.name),
                "icon": _icon_for(plugin.name),
                "accent": ds.accent(),
                "kind": tr("插件", "Plugin"),
            })
    except Exception:  # noqa: BLE001 - 第三方插件异常不应拖垮首页
        pass
    return candidates


class _MiniIcon(QWidget):
    """小圆角图标块。"""

    def __init__(self, icon, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(40, 40)
        self._icon = IconWidget(icon, self)
        self._icon.setFixedSize(20, 20)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._icon, 0, Qt.AlignCenter)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(self._color)
        color.setAlpha(42 if not ds.is_dark() else 55)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(self.rect(), 12, 12)


class QuickFunctionItem(QWidget):
    """工具坞中的扁平快捷入口；悬停反馈不再制造嵌套卡片。"""

    clicked = Signal()

    def __init__(self, icon, title, accent, shortcut_id=None, parent=None):
        super().__init__(parent)
        self.shortcut_id = shortcut_id
        self.nav_key = shortcut_id  # 保留旧调用方使用的属性名
        self._hovered = False
        self._pressed = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(72)
        self.setFixedHeight(76)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(title)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 6)
        layout.setSpacing(4)
        self._icon_box = _MiniIcon(icon, accent, self)
        layout.addWidget(self._icon_box, 0, Qt.AlignHCenter)
        self.title_label = CaptionLabel(title, self)
        self.title_label.setStyleSheet(
            "font-size: 12px; font-weight: 600; border: none; background: transparent;")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

    def enterEvent(self, event):
        self._hovered = True
        self.title_label.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {ds.accent()};"
            "border: none; background: transparent;")
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.title_label.setStyleSheet(
            "font-size: 12px; font-weight: 600; border: none; background: transparent;")
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        should_activate = (self._pressed and event.button() == Qt.LeftButton
                           and self.rect().contains(event.position().toPoint()))
        self._pressed = False
        self.update()
        if should_activate:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        """快捷入口支持 macOS 键盘导航与 Space/Return 激活。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        """绘制轻量 hover 与键盘焦点，不给每个入口增加独立阴影。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._hovered or self._pressed:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(
                ds.tokens()["card_active" if self._pressed else "card_hover"]))
            painter.drawRoundedRect(rect, 9, 9)
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(ds.accent()), 2))
            painter.drawRoundedRect(rect, 9, 9)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()


class _ShortcutEditorDialog(FluentDialogBase):
    """快捷入口选择器：搜索、勾选，最多保留六个入口。"""

    def __init__(self, candidates, selected_ids, parent=None):
        super().__init__(tr("自定义首页快捷功能", "Customize home shortcuts"), parent)
        self.resize(560, 620)
        self._candidates = candidates
        self._selected_ids = list(selected_ids)
        self._changing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)
        root.addWidget(StrongBodyLabel(
            tr("选择首页常用功能与插件", "Choose home features and plugins"), self))
        root.addWidget(CaptionLabel(
            tr("最多选择 6 项；首页始终只显示一行。",
               "Choose up to 6 items; the home row never wraps."), self))

        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText(tr("搜索功能或插件", "Search features or plugins"))
        self.search.textChanged.connect(self._apply_filter)
        root.addWidget(self.search)

        self.list_widget = QListWidget(self)
        self.list_widget.setAlternatingRowColors(False)
        self._items = []
        selected = set(selected_ids)
        for candidate in candidates:
            item = QListWidgetItem(
                candidate["icon"].icon(),
                f"{candidate['title']}   ·   {candidate['kind']}")
            item.setData(Qt.UserRole, candidate["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if candidate["id"] in selected else Qt.Unchecked)
            item.setSizeHint(QSize(0, 44))
            self.list_widget.addItem(item)
            self._items.append(item)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list_widget, 1)

        self.count_label = CaptionLabel("", self)
        root.addWidget(self.count_label)

        buttons = QHBoxLayout()
        self.reset_button = PushButton(tr("恢复默认", "Restore defaults"), self)
        self.reset_button.clicked.connect(self._restore_defaults)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        cancel_button = PushButton(tr("取消", "Cancel"), self)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        save_button = PrimaryPushButton(tr("保存", "Save"), self)
        save_button.clicked.connect(self._accept_selection)
        buttons.addWidget(save_button)
        root.addLayout(buttons)
        self._refresh_count()

    def _checked_ids(self):
        checked = {
            item.data(Qt.UserRole)
            for item in self._items if item.checkState() == Qt.Checked
        }
        # 先保留用户原有次序，新加入的入口再按候选列表次序附加。
        ordered = [key for key in self._selected_ids if key in checked]
        ordered.extend(
            item.data(Qt.UserRole) for item in self._items
            if item.checkState() == Qt.Checked and item.data(Qt.UserRole) not in ordered)
        return ordered[:MAX_SHORTCUTS]

    def _on_item_changed(self, item):
        if self._changing:
            return
        checked_count = sum(i.checkState() == Qt.Checked for i in self._items)
        if checked_count > MAX_SHORTCUTS:
            self._changing = True
            item.setCheckState(Qt.Unchecked)
            self._changing = False
        self._refresh_count()

    def _refresh_count(self):
        count = sum(item.checkState() == Qt.Checked for item in self._items)
        self.count_label.setText(
            tr("已选择 {} / {} 项", "{} / {} selected").format(count, MAX_SHORTCUTS))
        self.count_label.setStyleSheet(
            f"color: {ds.tokens()['ink_sec']}; background: transparent;")

    def _apply_filter(self, text):
        keyword = (text or "").strip().casefold()
        for item in self._items:
            item.setHidden(bool(keyword and keyword not in item.text().casefold()))

    def _restore_defaults(self):
        defaults = set(DEFAULT_SHORTCUTS)
        self._changing = True
        for item in self._items:
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in defaults
                               else Qt.Unchecked)
        self._changing = False
        self._selected_ids = list(DEFAULT_SHORTCUTS)
        self._refresh_count()

    def _accept_selection(self):
        self.result = self._checked_ids()
        self.accept()

class QuickFunctionRow(Card):
    """六个可配置入口加“更多”，始终保持为完整的单行工具坞。"""

    def __init__(self, services=None, parent=None):
        super().__init__(parent, radius=12)
        self.services = services
        self._nav_fn = None
        self._plugin_fn = None
        self._editor_dialog = None
        self._candidates = _shortcut_candidates()
        self._candidate_map = {item["id"]: item for item in self._candidates}
        saved = services.get_pref(PREF_KEY, DEFAULT_SHORTCUTS) if services else DEFAULT_SHORTCUTS
        self._shortcut_ids = self._normalize_ids(saved)

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(8, 7, 8, 7)
        self._grid.setHorizontalSpacing(3)
        self._grid.setVerticalSpacing(0)
        self._columns = 0
        self.items = []
        self.more_item = QuickFunctionItem(
            FluentIcon.MORE, tr("更多", "More"), ds.accent(), "more", self)
        self.more_item.setToolTip(
            tr("查看更多并编辑常用工具", "View and edit frequent tools"))
        self.more_item.clicked.connect(self._open_editor)
        # 保留旧测试及外部调用使用的属性名。
        self.edit_button = self.more_item
        self._rebuild()

    def _normalize_ids(self, ids):
        if not isinstance(ids, list):
            ids = DEFAULT_SHORTCUTS
        normalized = []
        for shortcut_id in ids:
            if (isinstance(shortcut_id, str)
                    and shortcut_id in self._candidate_map
                    and shortcut_id not in normalized):
                normalized.append(shortcut_id)
            if len(normalized) == MAX_SHORTCUTS:
                break
        return normalized

    def _rebuild(self):
        for item in self.items:
            self._grid.removeWidget(item)
            item.deleteLater()
        self.items = []
        self._columns = 0

        for shortcut_id in self._shortcut_ids:
            candidate = self._candidate_map[shortcut_id]
            item = QuickFunctionItem(
                candidate["icon"], candidate["title"], candidate["accent"],
                shortcut_id, self)
            item.clicked.connect(
                lambda checked=False, key=shortcut_id: self._activate(key))
            self.items.append(item)
        self._relayout(max(1, len(self.items)))

    def _activate(self, shortcut_id):
        if shortcut_id.startswith("plugin:"):
            if self._plugin_fn:
                self._plugin_fn(shortcut_id.removeprefix("plugin:"))
        elif self._nav_fn:
            self._nav_fn(shortcut_id)

    def _open_editor(self):
        # 连点两次会排队两个弹窗，这里去重。
        if self._editor_dialog is not None:
            self._editor_dialog.raise_()
            return
        dialog = _ShortcutEditorDialog(
            self._candidates, self._shortcut_ids, self.window())
        self._editor_dialog = dialog

        def _finished(result_code):
            selected = (list(dialog.result)
                        if result_code == QDialog.Accepted
                        and dialog.result is not None else None)
            if self._editor_dialog is dialog:
                self._editor_dialog = None
            dialog.deleteLater()
            if selected is not None:
                # 让原生弹窗先完整退出当前事件，再重建其下方的首页快捷行。
                QTimer.singleShot(
                    0, lambda values=selected: self.set_shortcuts(
                        values, persist=True))

        dialog.finished.connect(_finished)

        # 使用非阻塞 open()，避免 exec() 的 macOS 嵌套事件循环在关闭时
        # 先恢复父窗口、再刷新首页，形成用户可见的第二次闪烁。
        QTimer.singleShot(0, dialog.open)

    def open_editor(self):
        """供工具坞内的低权重编辑按钮调用。"""
        self._open_editor()

    def shortcut_ids(self):
        return list(self._shortcut_ids)

    def set_shortcuts(self, ids, persist=False):
        self._shortcut_ids = self._normalize_ids(ids)
        if persist and self.services:
            self.services.set_pref(PREF_KEY, list(self._shortcut_ids))
            flush = getattr(self.services.prefs, "flush", None)
            if callable(flush):
                flush()
        self._rebuild()

    def _relayout(self, columns):
        """无论窗口宽度如何，快捷入口都保持单行并均分剩余空间。"""
        columns = max(1, len(self.items))
        if columns == self._columns:
            return
        old_columns = self._columns
        self._columns = columns
        for index, item in enumerate(self.items):
            self._grid.removeWidget(item)
            self._grid.addWidget(item, 0, index)
        self._grid.removeWidget(self.more_item)
        self._grid.addWidget(self.more_item, 0, columns)
        for column in range(max(old_columns, columns, MAX_SHORTCUTS) + 1):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def connect_nav(self, nav_fn, plugin_fn=None):
        self._nav_fn = nav_fn
        self._plugin_fn = plugin_fn
