"""滚动性能回归测试：页面功能区滚轮必须原生即时滚动（无 400ms 动画延迟）。

背景：懒加载面板（BaseQtPanel 继承 qfluentwidgets ScrollArea）晚于启动时的
enable_smooth_scrolling 构建，SmoothScrollDelegate 保持 LINEAR 模式 →
滚轮事件进 FixedStepSmoothScrollEngine（400ms 逐帧动画）→ 每格滚轮
要 400ms 才滚完（表现为「页面滚轮滚动很慢」）。修复：apply_fast_scroll
处理 root 自身 + 全部子孙，NO_SMOOTH + ScrollPerPixel。
"""
import os
import sys
import gc
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"

import pytest

from PySide6.QtWidgets import (QApplication, QWidget, QAbstractScrollArea,
                               QTableWidget, QVBoxLayout)
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWheelEvent
from qfluentwidgets.common.smooth_scroll import SmoothMode


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_area(app):
    from qfluentwidgets import ScrollArea
    sa = ScrollArea()
    big = QWidget()
    big.setFixedHeight(3000)
    sa.setWidget(big)
    sa.setWidgetResizable(True)
    sa.resize(400, 300)
    sa.show()
    app.processEvents()
    sa.verticalScrollBar().setValue(1000)  # 定位到中间便于测双向滚动
    return sa


def _send_wheel(app, sa, delta):
    vp = sa.viewport()
    pos = QPoint(200, 150)
    ev = QWheelEvent(
        pos, sa.mapToGlobal(pos), QPoint(0, 0), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False)
    before = sa.verticalScrollBar().value()
    QApplication.sendEvent(vp, ev)
    app.processEvents()
    return before - sa.verticalScrollBar().value()


def test_panel_scroll_is_immediate(app):
    """面板滚动区域应用 apply_fast_scroll 后，滚轮立即滚动（NO_SMOOTH 原生）。"""
    from gui_qt.components import design_system as ds
    sa = _make_area(app)
    ds.apply_fast_scroll(sa)
    assert sa.scrollDelagate is not None
    assert sa.scrollDelagate.verticalSmoothScroll.smoothMode == SmoothMode.NO_SMOOTH
    moved = _send_wheel(app, sa, -120)  # 向下滚一格
    assert abs(moved) >= 30, f"滚轮一格应立即滚动 ≥30px，实际 {moved}px（疑似动画延迟）"


def test_scroll_boost_idempotent(app):
    """apply_fast_scroll 幂等：重复应用不重复处理（property 标记）。"""
    from gui_qt.components import design_system as ds
    sa = _make_area(app)
    n1 = ds.apply_fast_scroll(sa)
    n2 = ds.apply_fast_scroll(sa)
    assert n1 >= 1
    assert n2 == 0


def test_global_scroll_booster_lifetime_is_bound_to_application(app):
    """全局事件过滤器必须由 QApplication 强引用，避免 GC 后原生崩溃。"""
    from gui_qt.components import design_system as ds

    booster = ds.install_scroll_speed_booster(app)
    ref = weakref.ref(booster)
    del booster
    gc.collect()

    assert ref() is app._fm_scroll_speed_booster
    assert ref().parent() is app
    assert ds.install_scroll_speed_booster(app) is ref()


def test_global_scroll_booster_does_not_redispatch_recursively(app):
    """合成滚轮事件只放大一次，不能再次进入过滤器造成栈溢出。"""
    from gui_qt.components import design_system as ds

    sa = _make_area(app)
    ds.apply_fast_scroll(sa)
    ds.install_scroll_speed_booster(app)
    moved = _send_wheel(app, sa, -120)

    assert abs(moved) >= 30
    assert app._fm_scroll_speed_booster._dispatching_boosted_event is False


def test_nested_scroll_at_boundary_hands_wheel_to_page(app):
    """内层列表无可滚范围时，滚轮必须继续驱动外层长页面。"""
    from gui_qt.components import design_system as ds
    from qfluentwidgets import ScrollArea

    outer = ScrollArea()
    content = QWidget()
    content.setFixedHeight(1800)
    layout = QVBoxLayout(content)
    table = QTableWidget(1, 1, content)
    table.setFixedHeight(220)
    layout.addWidget(table)
    layout.addStretch(1)
    outer.setWidget(content)
    outer.setWidgetResizable(True)
    outer.resize(500, 360)
    outer.show()
    app.processEvents()

    ds.apply_fast_scroll(outer)
    ds.install_scroll_speed_booster(app)
    assert table.verticalScrollBar().maximum() == 0
    assert outer.verticalScrollBar().maximum() > 0

    outer.verticalScrollBar().setValue(0)
    moved = _send_wheel(app, table, -120)
    assert outer.verticalScrollBar().value() > 0, \
        "内层列表到底后应把向下滚动交给外层页面"
    assert moved == 0  # _send_wheel 返回的是内层列表自身的位移


def test_wheel_on_child_is_dispatched_to_sidebar_scroll_viewport(app):
    """菜单项即使消费滚轮事件，也不能阻断所属侧边栏滚动。"""
    from gui_qt.components import design_system as ds
    from qfluentwidgets import ScrollArea

    class _WheelEatingWidget(QWidget):
        def wheelEvent(self, event):
            event.accept()

    sidebar = ScrollArea()
    content = QWidget()
    content.setFixedHeight(1800)
    child = _WheelEatingWidget(content)
    child.setGeometry(0, 0, 180, 80)
    sidebar.setWidget(content)
    sidebar.setWidgetResizable(True)
    sidebar.resize(200, 320)
    sidebar.show()
    app.processEvents()

    ds.apply_fast_scroll(sidebar)
    ds.install_scroll_speed_booster(app)
    sidebar.verticalScrollBar().setValue(0)
    pos = QPoint(40, 40)
    event = QWheelEvent(
        pos, child.mapToGlobal(pos), QPoint(0, 0), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(child, event)
    app.processEvents()

    assert sidebar.verticalScrollBar().value() > 0, \
        "侧边栏子菜单接收滚轮时仍应驱动侧边栏滚动"

    # macOS 触控板通常只有 pixelDelta。该事件也必须绕过会消费滚轮的
    # 菜单子控件，送达侧边栏 viewport。
    sidebar.verticalScrollBar().setValue(0)
    pixel_event = QWheelEvent(
        pos, child.mapToGlobal(pos), QPoint(0, -40), QPoint(0, 0),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(child, pixel_event)
    app.processEvents()
    assert sidebar.verticalScrollBar().value() > 0, \
        "触控板在侧边栏菜单项上滑动时也应驱动侧边栏"


def test_panel_keeps_full_content_height_when_viewport_nearly_fits(app):
    """视口仅差少量高度时也必须可滚到底，不能压缩并裁掉末尾卡片。"""
    from PySide6.QtWidgets import QLayout
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.audio_panel import AudioPanelPage

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)

    class _Win:
        pass

    panel = AudioPanelPage(_Win(), services)
    panel.resize(1466, 919)
    panel.show()
    app.processEvents()

    assert panel.content_layout.verticalSizeConstraint() == \
        QLayout.SizeConstraint.SetFixedSize
    assert panel.content.height() >= panel.content_layout.sizeHint().height()
    # 密度优化后该尺寸可能已经完整容纳全部内容；只有溢出时才要求滚动，
    # 验收目标是末尾区域可达，而不是为了滚动而制造溢出。
    panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
    last_widget = next(
        panel.content_layout.itemAt(i).widget()
        for i in range(panel.content_layout.count() - 1, -1, -1)
        if panel.content_layout.itemAt(i).widget() is not None
    )
    visible_bottom = last_widget.geometry().bottom() - \
        panel.verticalScrollBar().value()
    assert visible_bottom <= panel.viewport().height()


def test_panel_build_applies_fast_scroll(app):
    """真实面板构建后自动应用快速滚动（BaseQtPanel.__init__ 内接线）。"""
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager
    services = QtServices()
    services.task_manager = TaskManager(services)
    from gui_qt.components.theme_manager import ThemeManager
    services.theme_mgr = ThemeManager(services)

    class _Win:
        pass

    from gui_qt import nav_registry as nr
    item = nr.find_item("video")
    panel = item["factory"](_Win(), services)
    try:
        assert isinstance(panel, QAbstractScrollArea)
        assert panel.scrollDelagate is not None
        assert panel.scrollDelagate.verticalSmoothScroll.smoothMode == SmoothMode.NO_SMOOTH, \
            "面板构建后应自动进入 NO_SMOOTH（否则滚轮慢）"
    finally:
        panel.deleteLater()


def test_lazy_page_applies_fast_scroll(app):
    """懒加载页面构建后对整个页面子树应用快速滚动。"""
    from gui_qt.components.sidebar import LazyPage
    from gui_qt import nav_registry as nr
    from qfluentwidgets.common.smooth_scroll import SmoothMode

    item = nr.find_item("pdf")  # 含多个嵌套滚动区域的重面板
    page = LazyPage(item["factory"], object(), None)

    class _FakeServices:
        prefs = None
        def get_pref(self, *a, **k):
            return None

    class _FakeWindow:
        services = _FakeServices()

    # 手动模拟 _ensure（绕过需要真实 services 的构建）
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager
    services = QtServices()
    services.task_manager = TaskManager(services)
    from gui_qt.components.theme_manager import ThemeManager
    services.theme_mgr = ThemeManager(services)
    page._window = _FakeWindow()
    page._services = services
    page._ensure()
    try:
        # 页面根（LazyPage 自身）+ 面板 + 嵌套滚动区都应被优化
        found = 0
        for sa in [page, page._real, *page.findChildren(QAbstractScrollArea)]:
            d = getattr(sa, "scrollDelagate", None)
            if d is not None:
                found += 1
                assert d.verticalSmoothScroll.smoothMode == SmoothMode.NO_SMOOTH
        assert found >= 1
    finally:
        page.deleteLater()
