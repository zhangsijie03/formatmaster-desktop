"""form_widgets — 统一参数表单组件（Prism 设计系统）。

所有功能面板的参数区换用本组件，统一视觉：
- FormSection：带图标的卡片区块（标题 + 内容）
- FormGrid：等宽参数网格（label 在上 / 控件在下，悬停高亮提示）
- FormItem：单个参数项（图标 + 标签 + 控件 + 悬停提示）
- CollapsibleSection：渐进式披露折叠区（▸/▾ 标题按钮 + 可展开内容）

风格：左对齐标签、等宽控件、区块内分组、hover 提示图标，
与首页 Prism 视觉语言一致。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)
from qfluentwidgets import CaptionLabel, FluentIcon, IconWidget, PushButton

from gui_qt.components import design_system as ds
from gui_qt.components.card import Card


class FormSection(Card):
    """带图标的参数区块卡片。"""

    def __init__(self, title, icon=None, parent=None):
        super().__init__(parent, radius=12)
        self.setObjectName("formSection")

        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(20, 18, 20, 20)
        self.v.setSpacing(14)

        # 区块标题（图标 + 文字）—— L2 区块标题 15px/700
        head = QHBoxLayout()
        head.setSpacing(8)
        if icon is not None:
            iw = IconWidget(icon, self)
            iw.setFixedSize(16, 16)
            iw.setStyleSheet(f"color: {ds.accent()};")
            head.addWidget(iw)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            "font-size: 16px; font-weight: 700;"
            "border: none; background: transparent;")
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.v.addLayout(head)

    def add_form(self, grid):
        """添加一个 FormGrid 内容区。"""
        self.v.addLayout(grid)

    def add_widget(self, w):
        self.v.addWidget(w)

    def add_layout(self, layout):
        """添加一个子布局（QHBoxLayout 等）。"""
        self.v.addLayout(layout)

    def add_spacing(self, h=6):
        self.v.addSpacing(h)


class FormGrid(QGridLayout):
    """等宽参数网格：每列 label 在上、控件在下。"""

    def __init__(self, columns=2):
        super().__init__()
        self._columns = columns
        self._max_columns = columns
        self._col = 0
        self._row = 0
        self._fields = []
        self._hidden_fields = set()
        self.setHorizontalSpacing(18)
        self.setVerticalSpacing(14)

    def add_field(self, label, control, icon=None, hint=None, colspan=1):
        """添加一个参数项。control 为任意 Qt 控件。"""
        col = self._col
        row = self._row
        # 标签（带可选图标）
        lbl_row = QHBoxLayout()
        lbl_row.setSpacing(4)
        if icon is not None:
            iw = IconWidget(icon, None)
            iw.setFixedSize(14, 14)
            iw.setStyleSheet(f";")
            lbl_row.addWidget(iw)
        lbl = CaptionLabel(label)
        lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600;"
            "border: none; background: transparent;")
        lbl_row.addWidget(lbl)
        # Qt 的 buddy 关系同时提供点击标签聚焦与辅助技术可识别名称。
        # 这比只画一段视觉标签更符合桌面表单的键盘使用习惯。
        lbl.setBuddy(control)
        if not control.accessibleName():
            control.setAccessibleName(str(label))
        if hint:
            tip = CaptionLabel("ⓘ")
            tip.setStyleSheet(
                f"font-size: 11px;"
                "border: none; background: transparent;")
            tip.setToolTip(hint)
            lbl_row.addWidget(tip)
        lbl_row.addStretch(1)
        self.addLayout(lbl_row, row, col, 1, colspan)
        # 控件
        if control.minimumHeight() < 36:
            control.setMinimumHeight(36)
        self.addWidget(control, row + 1, col, 1, colspan)
        self.setColumnStretch(col, 1)
        self._fields.append((lbl_row, control, colspan))
        # 下一个位置：右移一列，到底换行
        self._col += colspan
        if self._col >= self._columns:
            self._col = 0
            self._row += 2
        return control

    def set_columns(self, columns):
        """按可用宽度重排字段，窄窗口可从双列安全降为单列。"""
        columns = max(1, int(columns))
        if columns == self._columns:
            return
        self._columns = columns
        self._max_columns = max(self._max_columns, columns)
        self._reflow_fields()

    def set_field_visible(self, index, visible):
        """隐藏字段时同步移除其网格占位，并重排其余字段。"""
        if not 0 <= index < len(self._fields):
            return
        if visible:
            self._hidden_fields.discard(index)
        else:
            self._hidden_fields.add(index)
        label_layout, control, _span = self._fields[index]
        control.setVisible(visible)
        for item_index in range(label_layout.count()):
            widget = label_layout.itemAt(item_index).widget()
            if widget is not None:
                widget.setVisible(visible)
        self._reflow_fields()

    def _reflow_fields(self):
        """按当前列数和可见字段重建位置，不销毁任何控件。"""
        for col in range(self._max_columns):
            self.setColumnStretch(col, 0)
        for label_layout, control, _span in self._fields:
            self.removeItem(label_layout)
            self.removeWidget(control)
        row = 0
        col = 0
        for index, (label_layout, control, original_span) in enumerate(self._fields):
            if index in self._hidden_fields:
                continue
            span = min(original_span, self._columns)
            if col and col + span > self._columns:
                row += 2
                col = 0
            self.addLayout(label_layout, row, col, 1, span)
            self.addWidget(control, row + 1, col, 1, span)
            for field_col in range(col, col + span):
                self.setColumnStretch(field_col, 1)
            col += span
            if col >= self._columns:
                row += 2
                col = 0
        self._col = col
        self._row = row


class FormRow(QHBoxLayout):
    """水平排布的表单行（两个控件并排）。"""

    def __init__(self, label, control, hint=None):
        super().__init__()
        self.setSpacing(8)
        lbl = CaptionLabel(label)
        lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600;"
            "border: none; background: transparent;")
        self.addWidget(lbl)
        self.addWidget(control, 1)


class CollapsibleSection(QWidget):
    """渐进式披露折叠区：▸/▾ 标题按钮 + 可展开内容容器。

    用法：
        adv = CollapsibleSection(tr("高级设置", "Advanced"),
                                 hint=tr("通常无需调整", "Usually leave as-is"))
        adv.add_layout(FormGrid(...))   # 或 adv.add_widget(widget)
        card.add_widget(adv)
    默认折叠；点击标题按钮展开/收起。业务逻辑不变（控件仍在，仅视觉隐藏）。
    """

    def __init__(self, title, hint="", parent=None):
        super().__init__(parent)
        self._title = str(title)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.btn = PushButton(FluentIcon.SETTING, f"{self._title} ▸")
        self.btn.setObjectName("disclosureButton")
        self.btn.setCheckable(True)
        self.btn.clicked.connect(self._toggle)
        head.addWidget(self.btn)
        if hint:
            h = CaptionLabel(str(hint))
            h.setStyleSheet(
                f"font-size: 12px;"
                "border: none; background: transparent;")
            head.addWidget(h)
        head.addStretch(1)
        v.addLayout(head)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)
        self.content.hide()
        v.addWidget(self.content)

    def add_widget(self, w):
        self.content_layout.addWidget(w)

    def add_layout(self, layout):
        self.content_layout.addLayout(layout)

    def set_expanded(self, on):
        self.content.setVisible(on)
        self.btn.setText(f"{self._title} {'▾' if on else '▸'}")

    def _toggle(self, checked):
        self.set_expanded(bool(checked))
