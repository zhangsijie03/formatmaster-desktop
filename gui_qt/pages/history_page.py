"""history_page — 转换历史（Prism 设计系统）。

搜索 + 筛选 + 统计概览：顶部统计卡（总数/成功/失败/今日），
工具栏含搜索框、类型与结果筛选，表格展示记录。
"""
import os

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QBoxLayout, QHBoxLayout,
                               QGridLayout, QHeaderView,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, LineEdit,
                            PillPushButton, PushButton, ScrollArea, TableView)

from gui_qt.components import toast
from gui_qt.components import design_system as ds
from gui_qt.components.card import Card
from gui_qt.components.empty_state import EmptyState
from gui_qt.components.form_widgets import CollapsibleSection
from gui_qt.i18n import tr
from gui_qt.components.page_header import PageHeader
from utils.config import HistoryStatus

_COLS = [tr("时间", "Time"), tr("类型", "Type"),
         tr("源文件", "Source file"), tr("输出文件", "Output file"),
         tr("结果", "Result")]
_RESULTS = [tr("全部", "All"), tr("成功", "Success"), tr("失败", "Failed")]

# 当前导航中仍存在的功能类型白名单：历史记录里已删除功能的旧类型
# （如 LUT 调色、语音转字幕）不再显示在筛选下拉中。中英文都映射到
# 同一稳定中文键，切换界面语言后旧记录仍能筛选。
_HISTORY_TYPE_PAIRS = (
    ("视频转换", "Video Convert"), ("音频转换", "Audio Convert"),
    ("图片转换", "Image Convert"), ("文档转换", "Doc Convert"),
    ("GIF 转换", "GIF Convert"), ("场景化转换", "Scene Convert"),
    ("图像裁剪", "Image Crop"), ("视频转 GIF", "Video to GIF"),
    ("场景化处理", "Scene Process"), ("PDF 处理", "PDF Tools"),
    ("PDF 编辑", "PDF Edit"), ("封面裁剪", "Cover Crop"),
    ("视频处理", "Video Tools"), ("视频压缩", "Video Compress"),
    ("视频抽帧", "Frame Extract"), ("字幕提取", "Subtitle"),
    ("视频反挤压", "Unwarp"), ("音频处理", "Audio Tools"),
    ("音频增强", "Audio Enhance"), ("音频裁剪", "Audio Cut"),
    ("图片压缩", "Image Compress"), ("图片水印", "Image Watermark"),
    ("证件照换底色", "ID Photo"), ("图片拼接", "Image Merge"),
    ("OCR 识别", "OCR"), ("表格识别", "Table OCR"),
    ("视频缩略图", "Video Thumbnails"), ("哈希校验", "Hash Check"),
    ("文件夹监视", "Folder Watch"), ("文件安全", "File Security"),
    ("视频下载", "Video Download"), ("M3U8 下载", "M3U8 Download"),
)
_TYPE_KEY = {alias: zh for zh, en in _HISTORY_TYPE_PAIRS
             for alias in (zh, en)}
_TYPE_LABEL = {zh: tr(zh, en) for zh, en in _HISTORY_TYPE_PAIRS}


def _history_type_key(value):
    return _TYPE_KEY.get(str(value or ""))


def _history_type_label(value):
    key = _history_type_key(value)
    return _TYPE_LABEL.get(key, str(value or ""))


class _StatChip(Card):
    """顶部统计小卡片：数值 + 标题。"""

    def __init__(self, title, accent, parent=None):
        super().__init__(parent, radius=12)
        self.setMinimumHeight(64)
        from PySide6.QtWidgets import QVBoxLayout
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 10, 16, 10)
        v.setSpacing(2)
        self.value_label = CaptionLabel("0", self)
        self.value_label.setStyleSheet(
            f"font-size: 20px; font-weight: 700; color: {accent};"
            "border: none; background: transparent;")
        v.addWidget(self.value_label)
        self.title_label = CaptionLabel(title, self)
        self.title_label.setStyleSheet(
            f"font-size: 11px;"
            "border: none; background: transparent;")
        v.addWidget(self.title_label)

    def set_value(self, v):
        self.value_label.setText(str(v))


class HistoryPage(ScrollArea):
    """转换历史页：搜索 + 筛选 + 统计。"""

    def __init__(self, window, services, parent=None):
        super().__init__(parent)
        self.setObjectName("history")
        self.main_window = window
        self.services = services
        self._all_records = []
        self._mutating = False
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)

        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(24, 20, 24, 24)
        v.setSpacing(14)
        self.setWidget(content)
        content.setAutoFillBackground(False)

        # ── 页面标题 ───────────────────────────────
        v.addWidget(PageHeader(
            tr("转换历史", "History"), tr("搜索、筛选并统计所有转换记录", "Search, filter and summarize all records"),
            icon=FluentIcon.HISTORY))

        # ── 统计概览 ───────────────────────────────
        self.stats_widget = QWidget(self)
        self.stats_layout = QGridLayout(self.stats_widget)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setHorizontalSpacing(12)
        self.stats_layout.setVerticalSpacing(12)
        self.stat_total = _StatChip(tr("累计转换", "Total conversions"), ds.accent())
        self.stat_ok = _StatChip(tr("成功", "Success"), ds.tokens()["success"])
        self.stat_fail = _StatChip(tr("失败", "Failed"), ds.tokens()["error"])
        self.stat_today = _StatChip(tr("今日转换", "Today"), ds.tokens()["warn"])
        self._stat_cards = (self.stat_total, self.stat_ok, self.stat_fail,
                            self.stat_today)
        self._reflow_stats(4)
        v.addWidget(self.stats_widget)

        # ── 图表区：近 14 天趋势 + 类型分布 ──────────
        from gui_qt.components.visual_widgets import TrendChart, TypeChart
        self.charts_widget = QWidget(self)
        self.charts_layout = QBoxLayout(
            QBoxLayout.LeftToRight, self.charts_widget)
        charts_row = self.charts_layout
        charts_row.setContentsMargins(0, 0, 0, 0)
        charts_row.setSpacing(12)
        self.chart_trend = TrendChart()
        self.chart_type = TypeChart()
        for c in (self.chart_trend, self.chart_type):
            c.setFixedHeight(160)
            charts_row.addWidget(c, 1)
        # 以查找和打开历史结果为主，统计图表复用现有折叠组件按需展开。
        self.charts_section = CollapsibleSection(
            tr("统计图表", "Charts"),
            hint=tr("统计基于全部历史记录", "Statistics use all history records"))
        self.charts_section.btn.setIcon(FluentIcon.PIE_SINGLE)
        self.charts_section.add_widget(self.charts_widget)
        v.addWidget(self.charts_section)

        # ── 工具栏（搜索 + 筛选）──────────────────
        self.toolbar_widget = QWidget(self)
        self.toolbar_layout = QBoxLayout(
            QBoxLayout.LeftToRight, self.toolbar_widget)
        toolbar = self.toolbar_layout
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self.filter_widget = QWidget(self.toolbar_widget)
        filter_row = QHBoxLayout(self.filter_widget)
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.search_edit = LineEdit(self)
        self.search_edit.setPlaceholderText(tr("搜索文件名 / 类型…", "Search name / type…"))
        self.search_edit.setAccessibleName(tr("搜索历史记录", "Search history"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._refresh)
        filter_row.addWidget(self.search_edit, 1)

        self.type_combo = ComboBox(self)
        self.type_combo.setAccessibleName(tr("转换类型筛选", "Conversion type filter"))
        self.type_combo.addItem(tr("全部类型", "All types"), userData=None)
        self.type_combo.currentIndexChanged.connect(self._refresh)
        filter_row.addWidget(self.type_combo)
        self.btn_reset = PushButton(tr("重置筛选", "Reset filters"))
        self.btn_reset.clicked.connect(self._reset_filters)
        filter_row.addWidget(self.btn_reset)
        toolbar.addWidget(self.filter_widget, 1)

        # 结果筛选 → PillPushButton 标签组（全部/成功/失败）
        self.action_widget = QWidget(self.toolbar_widget)
        self.action_layout = QBoxLayout(
            QBoxLayout.LeftToRight, self.action_widget)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)
        result_widget = QWidget(self.action_widget)
        result_row = QHBoxLayout(result_widget)
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.setSpacing(8)
        self._result_val = tr("全部", "All")
        self.result_pills = []
        for label in _RESULTS:
            pill = PillPushButton(label, self)
            pill.setCheckable(True)
            pill.clicked.connect(
                lambda _=False, lab=label: self._on_result_pill(lab))
            result_row.addWidget(pill)
            self.result_pills.append(pill)
        self.result_pills[0].setChecked(True)

        self.count_label = CaptionLabel("")
        self.count_label.setStyleSheet(
            f"font-size: 12px;")
        result_row.addWidget(self.count_label)
        result_row.addStretch(1)
        self.action_layout.addWidget(result_widget)

        command_widget = QWidget(self.action_widget)
        command_row = QHBoxLayout(command_widget)
        command_row.setContentsMargins(0, 0, 0, 0)
        command_row.setSpacing(8)

        # 数据大屏（ECharts 交互图表，QtWebEngine 渲染）
        self.btn_dashboard = PushButton(FluentIcon.PIE_SINGLE,
                                        tr("数据大屏", "Dashboard"))
        self.btn_dashboard.setToolTip(
            tr("近 30 天转换趋势与类型分布（交互图表）",
               "30-day trend & type distribution (interactive)"))
        self.btn_dashboard.clicked.connect(self._open_dashboard)
        command_row.addWidget(self.btn_dashboard)

        self.btn_open = PushButton(
            FluentIcon.FOLDER, tr("打开输出", "Open output"))
        self.btn_open.setToolTip(
            tr("打开所选成功记录的输出文件",
               "Open the selected successful record output"))
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_selected)
        command_row.addWidget(self.btn_open)

        self.btn_delete = PushButton(
            FluentIcon.REMOVE, tr("删除选中", "Delete selected"))
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        command_row.addWidget(self.btn_delete)

        self.btn_clear = PushButton(FluentIcon.DELETE, tr("清空历史", "Clear history"))
        self.btn_clear.clicked.connect(self._clear)
        command_row.addWidget(self.btn_clear)
        self.action_layout.addWidget(command_widget)
        toolbar.addWidget(self.action_widget)
        v.addWidget(self.toolbar_widget)

        # ── 空态 ───────────────────────────────────
        self.empty_widget = QWidget()
        self.empty_widget.setLayout(EmptyState(
            icon=FluentIcon.HISTORY, title=tr("暂无转换记录", "No conversion records"),
            desc=tr("完成一次转换后，记录将在此显示", "Records will appear here after a conversion"),
            btn_text=tr("前往视频转换", "Go to Video Convert"),
            btn_clicked=lambda: self._goto("video")))
        v.addWidget(self.empty_widget, 1)

        self.no_results = EmptyState(
            icon=FluentIcon.SEARCH,
            title=tr("没有匹配的记录", "No matching records"),
            desc=tr("请调整搜索词或筛选条件",
                    "Adjust the search term or filters"),
            btn_text=tr("重置筛选", "Reset filters"),
            btn_clicked=self._reset_filters)
        self.no_results_widget = QWidget()
        self.no_results_widget.setLayout(self.no_results)
        self.no_results_widget.setVisible(False)
        v.addWidget(self.no_results_widget, 1)

        # ── 表格（qfw TableView + 排序模型）────────
        self._model = QStandardItemModel(0, len(_COLS))
        self._model.setHorizontalHeaderLabels(_COLS)
        self.table = TableView(self)
        self.table.setModel(self._model)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAccessibleName(tr("转换历史记录", "Conversion history records"))
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.verticalHeader().setVisible(False)
        # 点击表头排序（QStandardItemModel 内置 sort）
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(
            self._sync_selection_actions)
        self.table.doubleClicked.connect(lambda _index: self._open_selected())
        v.addWidget(self.table, 1)

        self._load_types()
        self._refresh()

    def _reset_filters(self):
        """一次恢复所有筛选，避免分别清空控件时重复重建表格。"""
        self.search_edit.blockSignals(True)
        self.type_combo.blockSignals(True)
        self.search_edit.clear()
        self.type_combo.setCurrentIndex(0)
        self.search_edit.blockSignals(False)
        self.type_combo.blockSignals(False)
        self._on_result_pill(_RESULTS[0])
        self.search_edit.setFocus()

    def _on_result_pill(self, label):
        """Pill 互斥：只保留当前选中的高亮，并刷新。"""
        for pill in self.result_pills:
            pill.setChecked(pill.text() == label)
        self._result_val = label
        self._refresh()

    def _load_types(self):
        """从历史记录收集类型填充筛选下拉，仅保留当前仍存在的功能。"""
        current_key = self.type_combo.currentData()
        seen = []
        for r in self.services.history.get_all():
            key = _history_type_key(r.get("type", ""))
            if key and key not in seen:
                seen.append(key)
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        self.type_combo.addItem(tr("全部类型", "All types"), userData=None)
        for key in seen:
            self.type_combo.addItem(_TYPE_LABEL[key], userData=key)
        if current_key in seen:
            self.type_combo.setCurrentIndex(
                self.type_combo.findData(current_key))
        self.type_combo.blockSignals(False)

    def _filtered(self):
        """按搜索词 + 类型 + 结果筛选记录。"""
        kw = self.search_edit.text().strip().lower()
        type_f = self.type_combo.currentData()
        result_f = self._result_val
        out = []
        for r in self._all_records:
            if kw:
                src = str(r.get("source", "")).lower()
                typ = _history_type_label(r.get("type", "")).lower()
                tgt = str(r.get("target", "")).lower()
                output = str(r.get("output_path", "")).lower()
                if (kw not in src and kw not in typ and kw not in tgt
                        and kw not in output):
                    continue
            if type_f and _history_type_key(r.get("type")) != type_f:
                continue
            if (result_f == tr("成功", "Success")
                    and r.get("status") != HistoryStatus.SUCCESS.value):
                continue
            if (result_f == tr("失败", "Failed")
                    and r.get("status") == HistoryStatus.SUCCESS.value):
                continue
            out.append(r)
        return out

    def _refresh(self):
        records = self.services.history.get_all()
        self._all_records = records
        self._update_stats(records)
        filtered = self._filtered()

        # 排序状态下清空会丢失排序状态：先禁用排序再重建
        self.table.setSortingEnabled(False)
        self._model.removeRows(0, self._model.rowCount())
        for r in filtered:
            is_success = r.get("status") == HistoryStatus.SUCCESS.value
            status = (tr("成功", "Success") if is_success
                      else tr("失败", "Failed"))
            out_name = os.path.basename(str(r.get("output_path", "")))
            tgt = out_name or r.get("target", "")
            values = [r.get("time", ""), _history_type_label(r.get("type", "")),
                      r.get("source", ""), tgt, status]
            items = []
            for c, val in enumerate(values):
                item = QStandardItem(str(val))
                item.setEditable(False)
                item.setData(dict(r), Qt.UserRole)
                if c == 2:
                    item.setToolTip(str(r.get("source", "")))
                elif c == 3:
                    item.setToolTip(str(r.get("output_path", "") or tgt))
                if c == 4:
                    t = ds.tokens()
                    fg = t["success"] if is_success else t["error"]
                    item.setForeground(QColor(fg))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif c == 0:
                    item.setForeground(QColor(ds.ink_sec()))
                items.append(item)
            self._model.appendRow(items)
        self.table.setSortingEnabled(True)
        self._sync_selection_actions()

        shown = len(filtered)
        total = len(records)
        self._sync_record_count()
        self.btn_reset.setEnabled(
            bool(self.search_edit.text()) or self.type_combo.currentIndex() > 0
            or self._result_val != _RESULTS[0])
        has = total > 0
        # 无数据时隐藏全 0 统计、空图表和无效筛选，把首要的
        # “开始第一次转换”直接放到标题下方。
        self.stats_widget.setVisible(has)
        self.charts_widget.setVisible(has)
        self.charts_section.setVisible(has)
        self.toolbar_widget.setVisible(has)
        self.empty_widget.setVisible(not has)
        no_match = has and shown == 0
        self.no_results_widget.setVisible(no_match)
        self.table.setVisible(has and not no_match)
        self.btn_clear.setEnabled(has and not self._mutating)

    def _update_stats(self, records):
        ok = sum(1 for r in records
                 if r.get("status") == HistoryStatus.SUCCESS.value)
        fail = len(records) - ok
        today = __import__("datetime").date.today().isoformat()
        today_n = sum(1 for r in records
                      if str(r.get("time", ""))[:10] == today)
        self.stat_total.set_value(len(records))
        self.stat_ok.set_value(ok)
        self.stat_fail.set_value(fail)
        self.stat_today.set_value(today_n)
        self._update_charts(records)

    def _update_charts(self, records):
        """刷新趋势图（近 14 天）与类型分布图。"""
        import datetime as _dt
        from collections import Counter
        days, ok_c, fail_c = [], [], []
        today = _dt.date.today()
        ok_counter, fail_counter = Counter(), Counter()
        for r in records:
            ds_ = str(r.get("time", ""))[:10]
            if r.get("status") == HistoryStatus.SUCCESS.value:
                ok_counter[ds_] += 1
            else:
                fail_counter[ds_] += 1
        for i in range(13, -1, -1):
            d = (today - _dt.timedelta(days=i)).isoformat()
            days.append(d[5:])
            ok_c.append(ok_counter.get(d, 0))
            fail_c.append(fail_counter.get(d, 0))
        self.chart_trend.set_data(days, ok_c, fail_c)
        type_counter = Counter(
            _history_type_label(r.get("type", tr("未知", "Unknown")))
            for r in records)
        self.chart_type.set_data(type_counter.most_common(7))

    def _open_dashboard(self):
        """ECharts 交互数据大屏：近 30 天趋势 + 类型分布。"""
        from collections import Counter
        import datetime as _dt
        records = self.services.history.get_all()
        if not records:
            from gui_qt.components import toast
            toast.show_info(self, tr("暂无转换记录", "No conversion records"))
            return
        today = _dt.date.today()
        ok_c, fail_c = Counter(), Counter()
        type_c = Counter(
            _history_type_label(r.get("type", tr("未知", "Unknown")))
            for r in records)
        for r in records:
            d = str(r.get("time", ""))[:10]
            if r.get("status") == HistoryStatus.SUCCESS.value:
                ok_c[d] += 1
            else:
                fail_c[d] += 1
        days, ok_l, fail_l = [], [], []
        for i in range(29, -1, -1):
            d = (today - _dt.timedelta(days=i)).isoformat()
            days.append(d[5:])
            ok_l.append(ok_c.get(d, 0))
            fail_l.append(fail_c.get(d, 0))
        types, counts = [], []
        for t, c in type_c.most_common(7):
            types.append(t)
            counts.append(c)
        from gui_qt.components.echarts_view import EchartsStatsDialog
        dlg = EchartsStatsDialog(self.window())
        if not dlg.available():
            from gui_qt.components import toast
            toast.show_warning(self, tr("数据大屏不可用", "Dashboard unavailable"))
            dlg.deleteLater()
            return
        dlg.set_data(days, ok_l, fail_l, types, counts)
        dlg.exec()

    def _selected_records(self):
        """从所选行携带的数据取记录，表格排序后仍对应原记录。"""
        rows = self.table.selectionModel().selectedRows()
        records = []
        for index in rows:
            item = self._model.item(index.row(), 0)
            record = item.data(Qt.UserRole) if item is not None else None
            if isinstance(record, dict):
                records.append(record)
        return records

    def _sync_selection_actions(self, *_args):
        records = self._selected_records()
        self.btn_delete.setEnabled(bool(records) and not self._mutating)
        self.btn_open.setEnabled(
            not self._mutating and len(records) == 1
            and records[0].get("status") == HistoryStatus.SUCCESS.value
            and bool(str(records[0].get("output_path") or "").strip()))
        self.btn_clear.setEnabled(bool(self._all_records) and not self._mutating)
        self._sync_record_count()

    def _sync_record_count(self):
        shown, total = self._model.rowCount(), len(self._all_records)
        text = (tr("共 {} 条", "{} records").format(total) if shown == total
                else tr("显示 {} / {} 条", "Showing {} / {}").format(shown, total))
        selected = len(self.table.selectionModel().selectedRows())
        if selected:
            text += tr(" · 已选 {} 条", " · {} selected").format(selected)
        self.count_label.setText(text)

    def _open_selected(self):
        records = self._selected_records()
        if self._mutating or len(records) != 1:
            return
        raw_path = str(records[0].get("output_path") or "")
        # 双击和按钮共享校验，空路径不能被 abspath 转成工作目录。
        if records[0].get("status") != HistoryStatus.SUCCESS.value or not raw_path.strip():
            toast.show_info(self, tr("这条记录没有可打开的成功输出。",
                                     "This record has no successful output to open."))
            return
        path = os.path.abspath(raw_path)
        if not os.path.exists(path):
            toast.show_warning(
                self, tr("输出文件已移动或删除，请检查原保存目录",
                         "The output was moved or deleted; check its save folder"))
            return
        from utils.platform_utils import open_path
        if not open_path(path):
            toast.show_error(
                self, tr("无法打开输出，请检查文件权限",
                         "Could not open the output; check file permissions"))

    def _delete_selected(self):
        if self._mutating:
            return
        records = self._selected_records()
        if not records:
            return
        # 确认框的嵌套事件循环期间也要防重入，避免重复删除或与清空交叉。
        self._mutating = True
        self._sync_selection_actions()
        try:
            self._confirm_delete(records)
        finally:
            self._mutating = False
            self._sync_selection_actions()

    def _confirm_delete(self, records):
        from qfluentwidgets import MessageBox
        box = MessageBox(
            tr("删除所选历史记录？", "Delete selected history records?"),
            tr("将删除所选的 {} 条记录，不会删除输出文件。",
               "Delete the selected {} records? Output files are kept.").format(
                   len(records)), self)
        box.yesButton.setText(tr("删除记录", "Delete records"))
        box.cancelButton.setText(tr("取消", "Cancel"))
        if not box.exec():
            return
        try:
            removed = self.services.history.delete_records(records)
        except Exception as exc:  # noqa: BLE001
            toast.show_error(
                self, tr("删除失败，请检查数据目录权限：{}",
                         "Delete failed; check data-folder permissions: {}").format(exc))
            return
        self._load_types()
        self._refresh()
        toast.show_success(
            self, tr("已删除 {} 条历史记录",
                     "Deleted {} history records").format(removed))

    def _clear(self):
        if self._mutating or not self._all_records:
            return
        self._mutating = True
        self._sync_selection_actions()
        try:
            self._confirm_clear()
        finally:
            self._mutating = False
            self._sync_selection_actions()

    def _confirm_clear(self):
        from qfluentwidgets import MessageBox
        box = MessageBox(
            tr("清空历史记录？", "Clear all history?"),
            tr("将清空全部 {} 条历史记录（包括筛选外的记录），不可撤销。源文件和输出文件均会保留。",
               "Clear all {} records, including those outside the filter? This cannot be undone. Source and output files are kept.").format(len(self._all_records)),
            self)
        box.yesButton.setText(tr("清空", "Clear"))
        box.cancelButton.setText(tr("取消", "Cancel"))
        if box.exec():
            self._do_clear()

    def _do_clear(self):
        try:
            self.services.history.clear()
        except Exception as exc:  # noqa: BLE001
            toast.show_error(
                self, tr("清空失败，请检查数据目录权限：{}",
                         "Clear failed; check data-folder permissions: {}").format(exc))
            return
        self._load_types()
        self._reset_filters()
        toast.show_success(self, tr("历史记录已清空", "History cleared"))

    def _goto(self, nav_key):
        pages = getattr(self.main_window, "pages", {})
        page = pages.get(nav_key)
        if page is not None:
            self.main_window.switchTo(page)

    def showEvent(self, e):
        self._load_types()
        self._refresh()
        super().showEvent(e)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 820
        self.charts_layout.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.toolbar_layout.setDirection(QBoxLayout.TopToBottom)
        self.action_layout.setDirection(
            QBoxLayout.TopToBottom if event.size().width() < 1100
            else QBoxLayout.LeftToRight)
        self._reflow_stats(2 if narrow else 4)
        for chart in (self.chart_trend, self.chart_type):
            chart.setFixedHeight(170 if narrow else 160)
        self._refresh_responsive_layout()
        QTimer.singleShot(0, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        for layout in (self.charts_layout, self.toolbar_layout,
                       self.action_layout, self.stats_layout):
            layout.invalidate()
            layout.activate()
        content = self.widget()
        if content is not None:
            content.setMinimumWidth(0)
            content.updateGeometry()

    def _reflow_stats(self, columns):
        for index, card in enumerate(self._stat_cards):
            self.stats_layout.addWidget(
                card, index // columns, index % columns)
        for column in range(4):
            self.stats_layout.setColumnStretch(
                column, 1 if column < columns else 0)
