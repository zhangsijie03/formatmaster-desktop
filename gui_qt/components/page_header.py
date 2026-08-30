"""page_header — 页面统一标题组件（Prism 设计系统）。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, IconWidget, SubtitleLabel

from gui_qt.components import design_system as ds


class PageHeader(QWidget):
    """页面标题：标题信息在左，完整任务操作组在右。"""

    def __init__(self, title, subtitle="", icon=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 4, 0)
        outer.setSpacing(5)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        if icon is not None:
            iw = IconWidget(icon, self)
            iw.setFixedSize(20, 20)
            iw.setStyleSheet(f"color: {ds.accent()};")
            top.addWidget(iw, 0, Qt.AlignVCenter)

        v = QVBoxLayout()
        v.setSpacing(4)
        self.title_label = SubtitleLabel(title)
        self.title_label.setStyleSheet(
            "font-size: 22px; font-weight: 700; letter-spacing: 0;")
        v.addWidget(self.title_label)
        if subtitle:
            self.subtitle_label = CaptionLabel(subtitle)
            self.subtitle_label.setProperty("sec", True)
            self.subtitle_label.setStyleSheet(
                "font-size: 12px;")
            v.addWidget(self.subtitle_label)
        else:
            self.subtitle_label = None
        v.setAlignment(Qt.AlignVCenter)
        top.addLayout(v, 1)

        # 状态、进度和页面级主操作统一固定在标题右侧，形成一个紧凑的
        # 任务操作簇，避免状态在左、按钮在右造成视线往返。
        self.action_layout = QHBoxLayout()
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)
        self.action_layout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addLayout(self.action_layout, 0)
        outer.addLayout(top)
        self._outer_layout = outer
        self._external_subtitle = None

        # 保留次行插槽供特殊页面使用；标准任务页不再占用该区域。
        self.progress_layout = QHBoxLayout()
        self.progress_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_layout.setSpacing(0)
        outer.addLayout(self.progress_layout)

        self._has_subtitle = bool(subtitle)
        self._action_widgets = []
        self._actions_below = False
        self.setFixedHeight(60 if subtitle else 42)

    def set_title(self, text):
        self.title_label.setText(text)

    def set_subtitle(self, text):
        if self.subtitle_label is not None:
            self.subtitle_label.setText(text)

    def add_action(self, widget):
        """把页面级命令挂载到标题右侧，并保持控件原有信号连接。"""
        widget.setParent(self)
        self._action_widgets.append(widget)
        self.action_layout.addWidget(widget, 0, Qt.AlignVCenter)
        self._sync_responsive_actions()

    def add_progress(self, widget):
        """特殊页面可将扩展进度内容铺在标题第二行。"""
        widget.setParent(self)
        self.progress_layout.addWidget(widget, 1)
        self._refresh_height(has_progress=True)

    def adopt_subtitle(self, widget):
        """收纳面板原有说明文字，使进度真正位于标题区最下方。"""
        if widget is None or self._external_subtitle is widget:
            return
        widget.setParent(self)
        widget.setProperty("sec", True)
        self._outer_layout.insertWidget(1, widget)
        self._external_subtitle = widget
        self._refresh_height(has_progress=self.progress_layout.count() > 0)

    def _refresh_height(self, has_progress=False):
        has_description = self._has_subtitle or self._external_subtitle is not None
        if has_progress:
            self.setFixedHeight(98 if has_description else 76)
        else:
            self.setFixedHeight(60 if has_description else 42)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_responsive_actions()

    def _sync_responsive_actions(self):
        """窄窗口把完整执行组下移，避免与标题、副标题互相挤压。"""
        if not self._action_widgets:
            return
        move_below = self.width() < 820
        if move_below == self._actions_below:
            return
        source = self.action_layout if move_below else self.progress_layout
        target = self.progress_layout if move_below else self.action_layout
        for widget in self._action_widgets:
            source.removeWidget(widget)
            target.addWidget(widget, 1 if move_below else 0,
                             Qt.AlignRight | Qt.AlignVCenter)
        self._actions_below = move_below
        self._refresh_height(has_progress=move_below)
