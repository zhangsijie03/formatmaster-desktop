"""mediainfo_panel — 媒体信息检测面板（MediaInfo 风格）。

添加媒体文件（视频/音频/图片），双击或点击文件即在下方表格显示
详细的容器/编码/流信息（ffprobe 后台线程读取）。
"""

import os

from PySide6.QtCore import QIODevice, QSaveFile, Signal
from PySide6.QtGui import QGuiApplication
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QTableWidget, QTableWidgetItem)
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton

from gui_qt.components import toast
from gui_qt.components.form_widgets import FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.widgets import ActionBar, ActionStatusState, FileListCard

MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".ts", ".3gp", ".mp3", ".wav", ".aac", ".flac",
              ".ogg", ".m4a", ".wma", ".opus", ".amr", ".png", ".jpg",
              ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}

SECTION_NAMES = {
    "文件": tr("文件", "File"), "视频流": tr("视频流", "Video stream"),
    "音频流": tr("音频流", "Audio stream"),
    "字幕流": tr("字幕流", "Subtitle stream"),
    "数据流": tr("数据流", "Data stream"),
}
FIELD_NAMES = {
    "标题": tr("标题", "Title"), "容器格式": tr("容器格式", "Container"),
    "文件大小": tr("文件大小", "File size"), "时长": tr("时长", "Duration"),
    "总码率": tr("总码率", "Overall bitrate"),
    "编码软件": tr("编码软件", "Encoder"), "编码器": tr("编码器", "Codec"),
    "规格": tr("规格", "Profile"), "分辨率": tr("分辨率", "Resolution"),
    "像素格式": tr("像素格式", "Pixel format"), "帧率": tr("帧率", "Frame rate"),
    "码率": tr("码率", "Bitrate"), "色彩空间": tr("色彩空间", "Color space"),
    "色彩传输": tr("色彩传输", "Color transfer"), "位深": tr("位深", "Bit depth"),
    "语言": tr("语言", "Language"), "采样率": tr("采样率", "Sample rate"),
    "声道": tr("声道", "Channels"), "类型": tr("类型", "Type"),
}


class _InfoWorker(SafeWorker):
    """后台读取媒体信息，避免大文件 ffprobe 阻塞 UI。"""

    sig_done = Signal(str, object)  # (路径, sections 或 None)

    def __init__(self, fp, parent=None):
        super().__init__(parent)
        self._fp = fp

    def work(self):
        from core.mediainfo import get_mediainfo
        self.sig_done.emit(self._fp, get_mediainfo(self._fp))


class MediaInfoPanelPage(BaseQtPanel):
    """媒体信息页。"""

    panel_key = "mediainfo"

    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("媒体信息", "Media Info"),
            tr("查看视频/音频/图片的容器、编码器、分辨率、码率等详细信息",
               "Inspect container, codec, resolution, bitrate, and stream details"),
            FluentIcon.INFO)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("媒体文件", "Media files"),
                                      file_exts=MEDIA_EXTS)
        self.file_card.table.setHorizontalHeaderLabels([
            tr("文件名", "Name"), tr("大小", "Size"),
            tr("格式", "Format"), tr("读取状态", "Read status")])
        lay.addWidget(self.file_card)
        self.file_card.files_changed.connect(self._on_files_changed)
        self.file_card.file_double_clicked.connect(self._inspect)
        self.file_card.table.itemSelectionChanged.connect(
            self._inspect_selected)
        self.file_hint = CaptionLabel(tr(
            "可添加多个媒体文件，页面会自动读取当前选中项。分析仅读取元数据，不会修改源文件。",
            "Add multiple media files and select one to inspect automatically. Analysis reads metadata only and never changes source files."))
        self.file_hint.setWordWrap(True)
        lay.addWidget(self.file_hint)

        self.action_bar = ActionBar(tr("查看信息", "Inspect"), self)
        self.btn_inspect = self.action_bar.btn_go
        self.btn_inspect.setIcon(FluentIcon.INFO)
        self.btn_inspect.clicked.connect(self._inspect_first)
        self.lb_status = self.action_bar.status_label
        self.action_bar.set_status(
            tr("选择文件后自动读取", "Select a file to inspect"),
            ActionStatusState.IDLE)
        self.btn_inspect.setEnabled(False)
        lay.addWidget(self.action_bar)

        result_card = FormSection(tr("详细信息", "Details"), FluentIcon.INFO)
        self.result_hint = CaptionLabel(tr(
            "尚未读取。添加文件后，容器、编码和音视频流信息将显示在下方。",
            "No media loaded. Container, codec, and stream details will appear below."))
        self.result_hint.setWordWrap(True)
        result_card.add_widget(self.result_hint)
        result_actions = QHBoxLayout()
        result_actions.addStretch(1)
        self.btn_copy = PushButton(FluentIcon.COPY, tr("复制全部", "Copy all"))
        self.btn_copy.clicked.connect(self._copy_all)
        self.btn_export = PushButton(FluentIcon.SAVE, tr("导出文本", "Export text"))
        self.btn_export.clicked.connect(self._export_text)
        result_actions.addWidget(self.btn_copy)
        result_actions.addWidget(self.btn_export)
        result_card.add_layout(result_actions)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(
            [tr("字段", "Field"), tr("值", "Value")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(300)
        self.table.setAlternatingRowColors(True)
        self.table.setAccessibleName(tr("媒体详细信息", "Media details"))
        result_card.add_widget(self.table)
        lay.addWidget(result_card)

        self._workers = set()
        self._request_serial = 0
        self._current_path = ""
        self._sections = []
        self._sync_result_actions()

    # ── 交互 ────────────────────────────────────
    def _on_files_changed(self):
        files = self.file_card.files()
        if files:
            self._inspect(self._selected_file())
            return
        self._request_serial += 1
        for worker in self._workers:
            worker.stop()
        self._current_path = ""
        self._sections = []
        self._clear_table()
        self._sync_result_actions()
        self.result_hint.setText(tr(
            "尚未读取。添加文件后，容器、编码和音视频流信息将显示在下方。",
            "No media loaded. Container, codec, and stream details will appear below."))
        self.btn_inspect.setEnabled(False)
        self.action_bar.set_status(
            tr("选择文件后自动读取", "Select a file to inspect"),
            ActionStatusState.IDLE)

    def _selected_file(self):
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else self.file_card.table.currentRow()
        if 0 <= row < len(files):
            return files[row]
        return files[0] if files else ""

    def _inspect_selected(self):
        selected = self._selected_file()
        if selected and selected != self._current_path:
            self._inspect(selected)

    def _inspect_first(self):
        selected = self._selected_file()
        if selected:
            self._inspect(selected)

    def _inspect(self, fp):
        if not os.path.isfile(fp):
            self.btn_inspect.setEnabled(bool(self.file_card.files()))
            self._set_file_state(fp, tr("文件不存在", "Missing"))
            toast.show_warning(self, tr("文件不存在或已被移动", "File is missing or was moved"))
            return
        from utils.config import get_ffprobe_path
        if not get_ffprobe_path():
            self.btn_inspect.setEnabled(True)
            self._set_file_state(fp, tr("读取失败", "Failed"))
            self.action_bar.set_status(
                tr("FFprobe 不可用", "FFprobe unavailable"), ActionStatusState.ERROR)
            toast.show_error(
                self, tr("未找到 FFprobe，无法读取媒体信息",
                         "FFprobe was not found; media information cannot be read"))
            return
        self._request_serial += 1
        request_id = self._request_serial
        self._current_path = fp
        for worker in self._workers:
            worker.stop()
        self._clear_table()
        self._sections = []
        self._sync_result_actions()
        self.result_hint.setText(tr(
            "正在读取「{}」的容器与流信息…",
            "Reading container and stream details for {}...").format(
                os.path.basename(fp)))
        self.btn_inspect.setEnabled(False)
        self._set_file_state(fp, tr("读取中…", "Reading…"))
        self.action_bar.set_status(
            tr("正在读取信息…", "Reading info…"),
            ActionStatusState.RUNNING)
        worker = _InfoWorker(fp, self)
        self._workers.add(worker)
        worker.sig_done.connect(
            lambda path, sections, token=request_id:
            self._on_done(token, path, sections))
        worker.sig_error.connect(
            lambda message, token=request_id:
            self._on_error(token, message))
        worker.finished.connect(lambda current=worker: self._release_worker(current))
        worker.start()

    def _on_done(self, request_id, fp, sections):
        if request_id != self._request_serial or fp != self._selected_file():
            return
        self.btn_inspect.setEnabled(True)
        if not sections:
            self._set_file_state(fp, tr("读取失败", "Failed"))
            self.result_hint.setText(tr(
                "未能读取「{}」。请确认文件完整且格式受支持。",
                "Could not read {}. Check that the file is intact and supported.").format(
                    os.path.basename(fp)))
            self.action_bar.set_status(
                tr("无法读取媒体信息", "Cannot read media info"),
                ActionStatusState.ERROR)
            toast.show_warning(self, tr("无法读取该文件的媒体信息",
                                        "Cannot read media info"))
            return
        self._sections = sections
        self._show(sections)
        self._sync_result_hint(fp, sections)
        self._sync_result_actions()
        self._set_file_state(fp, tr("已读取", "Loaded"))
        self.action_bar.set_status(
            tr("已加载：", "Loaded: ") + os.path.basename(fp),
            ActionStatusState.SUCCESS)

    def _on_error(self, request_id, message):
        if request_id != self._request_serial:
            return
        self.btn_inspect.setEnabled(bool(self.file_card.files()))
        self._set_file_state(self._current_path, tr("读取失败", "Failed"))
        self.result_hint.setText(tr(
            "读取「{}」时发生错误，可重试或选择其他文件。",
            "An error occurred while reading {}. Retry or select another file.").format(
                os.path.basename(self._current_path)))
        self.action_bar.set_status(
            tr("读取媒体信息失败", "Failed to read media info"),
            ActionStatusState.ERROR)
        toast.show_error(
            self, tr("读取媒体信息失败：{}", "Failed to read media info: {}")
            .format(message))

    def _release_worker(self, worker):
        self._workers.discard(worker)
        worker.deleteLater()

    def _set_file_state(self, path, state):
        row = self.file_card.row_of_file(path)
        if row >= 0:
            self.file_card.set_row_state(row, state)

    # ── 展示 ────────────────────────────────────
    def _clear_table(self):
        self.table.setRowCount(0)

    def _show(self, sections):
        self._clear_table()
        rows = 0
        for title, pairs in sections:
            self.table.insertRow(rows)
            item = QTableWidgetItem(self._section_name(title))
            f = item.font()
            f.setBold(True)
            item.setFont(f)
            self.table.setItem(rows, 0, item)
            self.table.setSpan(rows, 0, 1, 2)
            rows += 1
            for k, v in pairs:
                self.table.insertRow(rows)
                self.table.setItem(
                    rows, 0, QTableWidgetItem(FIELD_NAMES.get(str(k), str(k))))
                self.table.setItem(rows, 1, QTableWidgetItem(str(v)))
                rows += 1

    def _sync_result_hint(self, fp, sections):
        """用流数量摘要建立结果层级，详细技术字段仍保留在原表格中。"""
        counts = {"video": 0, "audio": 0, "subtitle": 0, "data": 0}
        prefixes = {
            "video": "视频流", "audio": "音频流",
            "subtitle": "字幕流", "data": "数据流",
        }
        for title, _pairs in sections:
            title_text = str(title)
            for stream_type, prefix in prefixes.items():
                if title_text == prefix or title_text.startswith(prefix + " "):
                    counts[stream_type] += 1
                    break
        self.result_hint.setText(tr(
            "当前文件：{name}。视频流 {video}，音频流 {audio}，字幕流 {subtitle}，数据流 {data}。",
            "Current file: {name}. Video: {video}. Audio: {audio}. Subtitles: {subtitle}. Data: {data}.").format(
                name=os.path.basename(fp), **counts))

    @staticmethod
    def _section_name(title):
        title = str(title)
        for source, translated in SECTION_NAMES.items():
            if title == source:
                return translated
            if title.startswith(source + " "):
                return translated + title[len(source):]
        return title

    def _result_text(self):
        blocks = []
        for title, pairs in self._sections:
            blocks.append(f"[{self._section_name(title)}]")
            blocks.extend(
                f"{FIELD_NAMES.get(str(key), str(key))}: {value}"
                for key, value in pairs)
            blocks.append("")
        return "\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def _sync_result_actions(self):
        enabled = bool(self._sections)
        self.btn_copy.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)

    def _copy_all(self):
        text = self._result_text()
        if text:
            QGuiApplication.clipboard().setText(text)
            toast.show_success(self, tr("媒体信息已复制", "Media info copied"))

    def _export_text(self):
        if not self._sections:
            return
        default_name = os.path.splitext(os.path.basename(self._current_path))[0]
        path, _filter = QFileDialog.getSaveFileName(
            self, tr("导出媒体信息", "Export media info"),
            f"{default_name}_mediainfo.txt", tr("文本文件 (*.txt)", "Text files (*.txt)"))
        if not path:
            return
        output = QSaveFile(path)
        if not output.open(QIODevice.WriteOnly):
            toast.show_error(self, tr("无法写入导出文件", "Cannot write export file"))
            return
        output.write(self._result_text().encode("utf-8"))
        if not output.commit():
            toast.show_error(self, tr("导出文件保存失败", "Failed to save export file"))
            return
        toast.show_success(
            self, tr("已导出：{}", "Exported: {}").format(os.path.basename(path)))

    def closeEvent(self, event):
        for worker in list(self._workers):
            worker.stop()
            if not worker.wait(500):
                for signal in (worker.sig_done, worker.sig_error, worker.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                worker.setParent(None)
                worker.finished.connect(worker.deleteLater)
        self._workers.clear()
        super().closeEvent(event)
