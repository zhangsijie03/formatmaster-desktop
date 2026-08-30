"""home_page — 首页转换工作台。

布局优先服务于“立即开始转换”：文件启动区、单行工具坞、最近任务与
常用预设；工具与系统诊断收纳在底部运行状态栏。
"""
from gui_qt.i18n import tr
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, FluentIcon, IconWidget,
                            PushButton, ScrollArea)

from gui_qt.components import design_system as ds
from gui_qt.components.card import Card
from gui_qt.components.quick_function_row import QuickFunctionRow
from gui_qt.components.recent_tasks_table import RecentTasksTable
from gui_qt.components.tool_status_card import ToolStatusCard
from gui_qt.components.system_info_card import SystemInfoCard
from utils.panel_presets import PanelPresetStore


class SavedPresetsCard(Card):
    """首页预设入口，只读取既有预设，不复制设置页的存储逻辑。"""

    preset_selected = Signal(str)
    manage_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, radius=12)
        self.store = PanelPresetStore()
        self.setMinimumHeight(230)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(8)

        title = QLabel(tr("常用预设", "Saved presets"), self)
        title.setStyleSheet(
            "font-size: 15px; font-weight: 700; border: none; background: transparent;")
        root.addWidget(title)

        hint = CaptionLabel(
            tr("一键恢复常用参数组合", "Restore a saved parameter set"), self)
        hint.setStyleSheet(
            f"color: {ds.ink_sec()}; border: none; background: transparent;")
        root.addWidget(hint)

        self.content = QWidget(self)
        self.content.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 4, 0, 4)
        self.content_layout.setSpacing(6)
        root.addWidget(self.content, 1)

        self.manage_button = PushButton(
            FluentIcon.SETTING, tr("管理预设", "Manage presets"), self)
        self.manage_button.clicked.connect(self.manage_requested)
        root.addWidget(self.manage_button, 0, Qt.AlignRight)
        self.refresh()

    def refresh(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        names = self.store.list()[:4]
        if not names:
            empty = CaptionLabel(
                tr("暂无已保存预设，可在设置中创建",
                   "No saved presets. Create one in Settings"), self.content)
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"color: {ds.ink_sec()}; border: none; background: transparent;")
            self.content_layout.addWidget(empty, 1)
            return

        for name in names:
            button = PushButton(FluentIcon.LIBRARY, name, self.content)
            button.setMinimumHeight(36)
            button.setAccessibleName(
                tr("应用预设：{}", "Apply preset: {}").format(name))
            button.clicked.connect(
                lambda checked=False, preset=name: self.preset_selected.emit(preset))
            self.content_layout.addWidget(button)
        self.content_layout.addStretch(1)


class HomePage(QWidget):
    """首页转换工作台。"""

    def __init__(self, window, services, parent=None):
        super().__init__(parent)
        self.main_window = window
        self.services = services
        self.setAcceptDrops(True)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.inner = QWidget()
        self.inner.setMaximumWidth(1380)
        v = QVBoxLayout(self.inner)
        v.setContentsMargins(24, 18, 24, 24)
        v.setSpacing(14)

        self._build_welcome(v)
        self._build_quick_functions(v)
        self._build_workspace(v)
        self._build_environment(v)
        v.addStretch(1)

        self.scroll.setWidget(self.inner)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

        self._refresh_all()

    # ── 1. 文件启动区 ───────────────────────────
    def _build_welcome(self, v):
        from gui_qt.components.design_system import HeroBanner
        self.hero = HeroBanner(
            tr("开始新的转换", "Start a new conversion"),
            tr("拖入文件即可自动识别，也可以批量选择整个文件夹",
               "Drop a file for automatic routing, or choose a whole folder"),
            self)
        self.hero.action_requested.connect(self._choose_convert_file)
        self.hero.folder_requested.connect(self._choose_convert_folder)
        v.addWidget(self.hero)

    # ── 常用功能 ─────────────────────────────────
    def _build_quick_functions(self, v):
        self.quick_fns = QuickFunctionRow(self.services)
        self.quick_fns.connect_nav(self._nav_to, self._open_plugin_shortcut)
        v.addWidget(self.quick_fns)

    # ── 最近任务 + 已保存预设 ────────────────────
    def _build_workspace(self, v):
        self.workspace = QWidget(self)
        self.workspace.setStyleSheet("background: transparent;")
        self.workspace_grid = QGridLayout(self.workspace)
        self.workspace_grid.setContentsMargins(0, 0, 0, 0)
        self.workspace_grid.setHorizontalSpacing(14)
        self.workspace_grid.setVerticalSpacing(14)

        self.recent_tasks = RecentTasksTable(self.workspace)
        self.recent_tasks.setMinimumHeight(230)
        self.recent_tasks.btn_history.clicked.connect(
            lambda: self._nav_to("history"))
        self.recent_tasks.btn_clear.clicked.connect(self._clear_tasks)
        self._wire_task_badge()

        self.saved_presets = SavedPresetsCard(self.workspace)
        self.saved_presets.preset_selected.connect(self._apply_saved_preset)
        self.saved_presets.manage_requested.connect(
            lambda: self._nav_to("settings"))

        self._workspace_compact = None
        self._relayout_workspace(False)
        v.addWidget(self.workspace)

    # ── 默认折叠的运行环境 ───────────────────────
    def _build_environment(self, v):
        self.environment_card = Card(self, radius=12)
        outer = QVBoxLayout(self.environment_card)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(12)

        header = QHBoxLayout()
        icon = IconWidget(FluentIcon.DEVELOPER_TOOLS, self.environment_card)
        icon.setFixedSize(18, 18)
        header.addWidget(icon)
        title = QLabel(tr("运行状态", "Runtime status"), self.environment_card)
        title.setStyleSheet(
            "font-size: 14px; font-weight: 700; border: none; background: transparent;")
        header.addWidget(title)
        self.environment_summary = CaptionLabel("", self.environment_card)
        self.environment_summary.setStyleSheet(
            f"color: {ds.ink_sec()}; border: none; background: transparent;")
        header.addWidget(self.environment_summary)
        header.addStretch(1)
        self.btn_environment = PushButton(
            tr("诊断详情", "Diagnostics"), self.environment_card)
        self.btn_environment.setCheckable(True)
        self.btn_environment.toggled.connect(self._toggle_environment)
        header.addWidget(self.btn_environment)
        outer.addLayout(header)

        self.environment_content = QWidget(self.environment_card)
        self.environment_content.setStyleSheet("background: transparent;")
        self.main_grid = QGridLayout(self.environment_content)
        self.main_grid.setContentsMargins(0, 0, 0, 0)
        self.main_grid.setHorizontalSpacing(16)
        self.main_grid.setVerticalSpacing(16)
        self.tool_status = ToolStatusCard(autorefresh=False)
        self.sysinfo = SystemInfoCard()
        self._main_compact = None
        self._relayout_main(False)
        self.environment_content.hide()
        outer.addWidget(self.environment_content)
        v.addWidget(self.environment_card)
        self._refresh_status_summary()

    def _relayout_workspace(self, compact):
        if compact == self._workspace_compact:
            return
        self._workspace_compact = compact
        self.workspace_grid.removeWidget(self.recent_tasks)
        self.workspace_grid.removeWidget(self.saved_presets)
        self.workspace_grid.setColumnStretch(0, 1)
        self.workspace_grid.setColumnStretch(1, 0)
        if compact:
            self.workspace_grid.addWidget(self.recent_tasks, 0, 0)
            self.workspace_grid.addWidget(self.saved_presets, 1, 0)
        else:
            self.workspace_grid.addWidget(self.recent_tasks, 0, 0)
            self.workspace_grid.addWidget(self.saved_presets, 0, 1)
            self.workspace_grid.setColumnStretch(0, 7)
            self.workspace_grid.setColumnStretch(1, 3)

    def _relayout_main(self, compact):
        if compact == self._main_compact:
            return
        self._main_compact = compact
        self.main_grid.removeWidget(self.tool_status)
        self.main_grid.removeWidget(self.sysinfo)
        if compact:
            self.main_grid.addWidget(self.tool_status, 0, 0, Qt.AlignTop)
            self.main_grid.addWidget(self.sysinfo, 1, 0, Qt.AlignTop)
            self.sysinfo.set_horizontal(False)
            self.main_grid.setColumnStretch(0, 1)
            self.main_grid.setColumnStretch(1, 0)
        else:
            self.main_grid.addWidget(self.tool_status, 0, 0, Qt.AlignTop)
            self.main_grid.addWidget(self.sysinfo, 0, 1, Qt.AlignTop)
            self.sysinfo.set_horizontal(False)
            self.main_grid.setColumnStretch(0, 3)
            self.main_grid.setColumnStretch(1, 7)

    def _toggle_environment(self, expanded):
        """诊断信息按需展开，避免长期占据首页主任务区域。"""
        self.environment_content.setVisible(expanded)
        self.btn_environment.setText(
            tr("收起", "Hide details") if expanded
            else tr("诊断详情", "Diagnostics"))
        if expanded:
            self._refresh_environment()

    def resizeEvent(self, event):
        """信息块按可用宽度重排，保证窗口缩小时仍有清晰层级。"""
        width = event.size().width()
        self._relayout_workspace(width < 980)
        self._relayout_main(width < 980)
        super().resizeEvent(event)

    def _nav_to(self, key):
        page = self.main_window.pages.get(key)
        if page:
            self.main_window.switchTo(page)

    def _route_convert_file(self, path):
        """复用应用统一文件路由；无法识别时给出明确的下一步。"""
        from gui_qt.app import _auto_open_convert_file
        if _auto_open_convert_file(self.main_window, path):
            return
        from gui_qt.components import toast
        toast.show_warning(self, tr(
            "暂不支持该文件类型，请从常用功能中选择对应工具",
            "This file type is not supported; choose a matching tool below"))

    def _choose_convert_file(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, tr("选择要转换的文件", "Choose a file to convert"))
        if path:
            self._route_convert_file(path)

    def _choose_convert_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, tr("选择要批量处理的文件夹", "Choose a folder to process"))
        if path:
            self._route_convert_folder(path)

    def _route_convert_folder(self, path):
        """文件夹先进入格式检测，用户确认分类后再批量添加到对应工具。"""
        page = self.main_window.pages.get("format_detect")
        if not page:
            return
        real_page = page._ensure() if hasattr(page, "_ensure") else page
        if hasattr(real_page, "ed_path"):
            real_page.ed_path.setText(path)
        self.main_window.switchTo(page)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() and os.path.exists(url.toLocalFile())
               for url in urls):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        """首页一次只处理首个本地路径，文件夹交给格式检测统一分类。"""
        for url in event.mimeData().urls():
            path = url.toLocalFile() if url.isLocalFile() else ""
            if path and os.path.isdir(path):
                self._route_convert_folder(path)
                event.acceptProposedAction()
                return
            if path and os.path.isfile(path):
                self._route_convert_file(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def _open_plugin_shortcut(self, plugin_id):
        """从首页直达插件面板，仍复用插件中心的加载和异常处理。"""
        page = self.main_window.pages.get("plugins")
        if not page:
            return
        real_page = page._ensure() if hasattr(page, "_ensure") else page
        real_page.open_plugin(plugin_id, self.window())

    def _apply_saved_preset(self, name):
        """只实例化预设实际包含的面板，再批量恢复其参数。"""
        from gui_qt.components import toast

        panels = self.saved_presets.store.load(name)
        if not panels:
            toast.show_error(
                self, tr("预设不存在或已损坏", "Preset missing or corrupted"))
            self.saved_presets.refresh()
            return

        applied = 0
        failed = 0
        for panel_key, prefs in panels.items():
            page = self.main_window.pages.get(panel_key)
            if page is None:
                continue
            real_page = page._ensure() if hasattr(page, "_ensure") else page
            apply = getattr(real_page, "apply_prefs", None)
            if not callable(apply):
                continue
            try:
                apply(prefs)
                applied += 1
            except Exception:  # noqa: BLE001 - 单个面板失败不影响其余预设
                failed += 1
        if failed:
            toast.show_warning(
                self, tr("已应用 {} 个工具，{} 个失败",
                         "Applied to {} tools; {} failed").format(applied, failed))
            return
        toast.show_success(
            self, tr("已应用预设：{}", "Preset applied: {}").format(name))

    # ── 首页「最近任务」进行中数徽章（InfoBadge）──
    def _wire_task_badge(self):
        """连接 TaskManager 状态信号 → 实时刷新徽章。"""
        try:
            mgr = self.services.task_manager
            mgr.sig_state.connect(self._on_task_state_changed)
            self._refresh_active_badge()
        except Exception:  # noqa: BLE001 - 徽章接线失败不影响首页
            pass

    def _refresh_active_badge(self, *args):
        """进行中任务数（等待/运行/暂停），0 时隐藏徽章。"""
        try:
            n = 0
            for task in self.services.task_manager.all_tasks():
                if task.state in ("waiting", "processing", "paused"):
                    n += 1
            self.recent_tasks.badge_active.setText(str(n))
            self.recent_tasks.badge_active.setVisible(n > 0)
        except Exception:  # noqa: BLE001 - 徽章刷新失败不影响首页
            pass

    def _on_task_state_changed(self, *_args):
        """任务状态变化时同步首页，而不是等用户切走再切回才刷新。"""
        self._refresh_active_badge()
        self._refresh_tasks()

    def _clear_tasks(self):
        if getattr(self, "_clear_dialog", None) is not None:
            self._clear_dialog.raise_()
            return
        from qfluentwidgets import MessageDialog
        dialog = MessageDialog(
            tr("清除已结束任务", "Clear finished tasks"),
            tr("只会清除已成功、失败或取消的任务，不影响正在处理的任务。",
               "Completed, failed and cancelled tasks will be removed; active tasks are kept."),
            self.window())
        dialog.yesButton.setText(tr("清除", "Clear"))
        dialog.cancelButton.setText(tr("取消", "Cancel"))
        self._clear_dialog = dialog

        def _finished(result_code):
            if self._clear_dialog is dialog:
                self._clear_dialog = None
            dialog.deleteLater()
            if result_code != QDialog.Accepted:
                return
            try:
                self.services.task_manager.clear_completed()
                self._refresh_tasks()
                self._refresh_active_badge()
            except Exception as exc:  # noqa: BLE001
                from gui_qt.components import toast
                toast.show_error(self, tr("清除失败：{}", "Clear failed: {}").format(exc))

        dialog.finished.connect(_finished)
        dialog.open()

    # ── 数据刷新 ─────────────────────────────────
    def _refresh_all(self):
        self._refresh_tasks()
        self.saved_presets.refresh()
        self._refresh_status_summary()

    def _refresh_tasks(self):
        try:
            all_tasks = self.services.task_manager.all_tasks()
            tasks = all_tasks[:6]
        except Exception:
            all_tasks = []
            tasks = []
        self.recent_tasks.set_tasks(tasks)
        self.recent_tasks.btn_clear.setEnabled(any(
            task.state in ("success", "failed", "cancelled")
            for task in all_tasks))

    def showEvent(self, e):
        self._refresh_all()
        # 运行环境默认折叠；只有用户主动展开后才刷新诊断数据，避免首页
        # 每次显示都执行与当前任务无关的系统探测。
        if self.btn_environment.isChecked():
            self._refresh_environment()
        super().showEvent(e)

    def _refresh_environment(self):
        """按需刷新工具与系统信息，失败不影响首页的转换主流程。"""
        try:
            self.tool_status.refresh()
            self.sysinfo.refresh()
            self._refresh_status_summary(self.services.ffmpeg_ready())
        except Exception:  # noqa: BLE001 - 采集失败不影响首页
            pass

    def _refresh_status_summary(self, ffmpeg_ready=None):
        """折叠状态栏只展示关键结论，详细探测仍由用户主动触发。"""
        output_dir = getattr(self.services, "last_output_dir", "") or ""
        output_text = (os.path.basename(output_dir.rstrip(os.sep))
                       if output_dir else tr("跟随工具设置", "per-tool setting"))
        if ffmpeg_ready is None:
            engine_text = tr("转换引擎按需检测", "Engine checked on demand")
        elif ffmpeg_ready:
            engine_text = tr("FFmpeg 可用", "FFmpeg available")
        else:
            engine_text = tr("FFmpeg 需要配置", "FFmpeg needs setup")
        self.environment_summary.setText(
            tr("{} · 输出：{}", "{} · Output: {}").format(
                engine_text, output_text))

    def save_prefs(self):
        pass
