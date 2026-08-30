"""crop_panel — 图像预设裁剪面板（阶段2 迁移自 gui/panels/crop_panel.py）。

按社交媒体尺寸批量裁剪图片。整批文件作为单个任务执行
core.image_cropper.batch_crop（PIL 实现，不依赖 FFmpeg），输出为目录。
"""
import os

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen
from PySide6.QtWidgets import QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, PushButton

import core.image_cropper as ic
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components import design_system as ds
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt import task_manager as tm
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

# 预置值（与 tkinter 版 crop_panel 一致）
MODE_VALUES = [
    tr("cover（裁剪填充）", "cover (crop to fill)"),
    tr("fit（完整留白）", "fit (contain with padding)"),
]
DEFAULT_PRESET = tr("1:1 正方形 (1080×1080)", "1:1 Square (1080×1080)")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CROP_COVER = "cover"
CROP_FIT = "fit"
CROP_MODES = (CROP_COVER, CROP_FIT)
PREVIEW_MAX_PIXELS = 40_000_000
PREVIEW_SIZE = QSize(640, 480)


class _AspectPreview(QWidget):
    """沿用比例示意组件，按需显示缩略图，切换参数无需重复解码。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._size = (1080, 1080)
        self._source = QImage()
        self._mode = CROP_COVER
        self.setMinimumHeight(180)
        self.setAccessibleName(tr("输出画布比例预览", "Output aspect ratio preview"))

    def set_preset_size(self, size):
        self._size = tuple(size)
        self.update()

    def set_source(self, source: QImage) -> None:
        self._source = source
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin = 8.0
        available_w = max(1.0, self.width() - margin * 2)
        # 尺寸文字独立放在画布下方，竖版预设也不会将文字挤出边界。
        caption_height = self.fontMetrics().height() + margin
        available_h = max(1.0, self.height() - margin * 2 - caption_height)
        target_w, target_h = self._size
        scale = min(available_w / target_w, available_h / target_h)
        width = target_w * scale
        height = target_h * scale
        rect = QRectF((self.width() - width) / 2,
                      margin + (available_h - height) / 2, width, height)
        fill = self.palette().highlight().color()
        fill.setAlpha(28)
        border = self.palette().highlight().color()
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.5))
        painter.drawRoundedRect(rect, 8, 8)
        if not self._source.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.fillRect(rect, Qt.white)
            source = QRectF(self._source.rect())
            if self._mode == CROP_COVER:
                # 与引擎一致：居中裁剪，不提供会改变批处理语义的拖动裁剪框。
                source_ratio = source.width() / source.height()
                target_ratio = target_w / target_h
                if source_ratio > target_ratio:
                    cropped_w = source.height() * target_ratio
                    source.setLeft((source.width() - cropped_w) / 2)
                    source.setWidth(cropped_w)
                else:
                    cropped_h = source.width() / target_ratio
                    source.setTop((source.height() - cropped_h) / 2)
                    source.setHeight(cropped_h)
                painter.drawImage(rect, self._source, source)
            else:
                scale = min(rect.width() / source.width(), rect.height() / source.height())
                fitted = QRectF(0, 0, source.width() * scale, source.height() * scale)
                fitted.moveCenter(rect.center())
                painter.drawImage(fitted, self._source, source)
        painter.setPen(QColor(ds.ink()))
        painter.drawText(QRectF(0, self.height() - caption_height, self.width(), caption_height), Qt.AlignCenter,
                         f"{target_w} × {target_h}")


class CropPanelPage(BaseQtPanel, TaskPanelMixin):
    """图像预设裁剪页。"""

    panel_key = "video_edit"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("封面裁剪", "Cover crop"),
            tr("按常用平台比例批量裁剪图片，输出 JPG 且不会覆盖源文件",
               "Batch crop for common platforms; exports JPG without overwriting sources"),
            FluentIcon.CUT)
        lay.addWidget(self.header)
        self.btn_preview = PushButton(FluentIcon.VIEW, tr("预览裁剪", "Preview crop"))
        self.btn_preview.clicked.connect(self._preview_crop)
        self.header.add_action(self.btn_preview)
        self._preview_path = ""

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=IMAGE_EXTS)
        lay.addWidget(self.file_card)

        from gui_qt.components.form_widgets import FormGrid, FormSection
        card = FormSection(tr("裁剪参数", "Crop settings"), FluentIcon.CUT)
        self.params_grid = FormGrid(columns=2)
        self.cb_preset = ComboBox()
        self._preset_items = list(ic.PRESETS.items())
        self.cb_preset.addItems([name for name, _size in self._preset_items])
        self.cb_preset.setCurrentText(DEFAULT_PRESET)
        self.params_grid.add_field(
            tr("预设尺寸", "Preset size"), self.cb_preset,
            hint=tr("选择最终输出的像素尺寸", "Final output dimensions"))
        self.cb_mode = ComboBox()
        self.cb_mode.addItems(MODE_VALUES)
        self.cb_mode.setCurrentIndex(0)
        self.params_grid.add_field(
            tr("适配方式", "Fit mode"), self.cb_mode,
            hint=tr("cover 会裁掉边缘；fit 会完整保留并添加白边",
                    "cover crops edges; fit preserves the full image with padding"))
        self.aspect_preview = _AspectPreview()
        self.params_grid.add_field(
            tr("输出画布", "Output canvas"), self.aspect_preview,
            hint=tr("点击顶部“预览裁剪”查看选中图片；未加载时仅示意比例",
                    "Click Preview crop for the selected image; otherwise only the aspect ratio is shown"),
            colspan=2)
        card.add_form(self.params_grid)
        self.preview_hint = CaptionLabel()
        self.preview_hint.setWordWrap(True)
        card.add_widget(self.preview_hint)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        card.add_widget(self.mode_hint)
        lay.addWidget(card)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始裁剪", "Crop"))
        lay.addWidget(self.action_bar)

        self.cb_preset.currentIndexChanged.connect(self._sync_preset_preview)
        self.cb_mode.currentIndexChanged.connect(self._sync_preset_preview)
        self.file_card.files_changed.connect(self._sync_preview_source)
        self.file_card.table.itemSelectionChanged.connect(self._sync_preview_source)
        self._wire_tasks()
        self._sync_preset_preview()
        self._sync_preview_source()

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        index = self.cb_preset.currentIndex()
        preset_name, preset_size = self._preset_items[
            index if 0 <= index < len(self._preset_items) else 0]
        return {
            "preset": preset_name,
            "preset_size": list(preset_size),
            # 简化模式名（与 _run_task_general 的 crop 分支读取键一致）
            "crop_mode": CROP_MODES[max(0, self.cb_mode.currentIndex())],
        }

    def collect_prefs(self) -> dict:
        return {
            "preset": self.cb_preset.currentText(),
            # mode 为原始字符串，用于恢复 ComboBox 选择
            "mode": self.cb_mode.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("preset") in ic.PRESETS:
            self.cb_preset.setCurrentText(prefs["preset"])
        if prefs.get("mode") in MODE_VALUES:
            self.cb_mode.setCurrentText(prefs["mode"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        size_raw = p.get("preset_size")
        sz = tuple(size_raw) if isinstance(size_raw, (list, tuple)) else None
        sz = sz or ic.PRESETS.get(p.get("preset", ""))
        if not sz:
            task.error = tr("未知预设：{}", "Unknown preset: {}").format(p.get('preset', ''))
            return False
        files_all = p.get("files") or [task.file_path]
        cnt = ic.batch_crop(files_all, task.output_path, sz,
                            p.get("crop_mode", "cover"), prog)
        if cnt != len(files_all):
            task.error = tr("仅成功裁剪 {}/{} 个文件",
                            "Only {}/{} files were cropped").format(
                                cnt, len(files_all))
            return False
        return True

    # ── 任务提交（整批单任务，输出为目录）──────────
    def _start(self):
        # 本页整批作为一个任务提交；在活动批次结束前阻止再次入队。
        if any(task and task.state in (tm.WAITING, tm.RUNNING, tm.PAUSED)
               for task in (self.services.task_manager.get_task(tid)
                            for tid in self._task_rows)):
            toast.show_info(self, tr("当前裁剪任务尚未结束，请稍后再试", "Wait for the current crop task to finish"))
            return False
        files = self.file_card.files()
        if not files:
            toast.show_warning(self, tr("请先添加要裁剪的图片", "Add images to crop first"))
            return False
        if self.out_row.mode() == OutputDirRow.MODE_CUSTOM and not self.out_row.path():
            toast.show_warning(self, tr("请先选择自定义输出目录", "Choose an output folder first"))
            return False

        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(files[0])
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            toast.show_error(self, tr("无法创建输出目录：{}", "Cannot create output folder: {}").format(out_dir))
            return False

        self.save_prefs()
        params["files"] = list(files)
        mgr = self.services.task_manager
        tid = mgr.add_task(
            name=tr("图像裁剪 - {}个文件", "Image Crop - {} files").format(len(files)),
            task_type="crop", file_path=files[0], output_path=out_dir,
            params=params, runner=self._runner,
            history_type=tr("图像裁剪", "Image Crop"), history_target=params["preset"],
            need_ffmpeg=False)
        if tid is not None:
            self._task_rows[tid] = (files[0], -1)
            self.action_bar.set_running(True)
            self.action_bar.set_status(tr("已提交裁剪任务", "Crop task submitted"))
            return True
        else:
            toast.show_error(self, tr("任务提交失败", "Submit failed"))
            return False

    def _sync_preset_preview(self, *_args):
        index = self.cb_preset.currentIndex()
        if not (0 <= index < len(self._preset_items)):
            return
        _name, size = self._preset_items[index]
        self.aspect_preview.set_preset_size(size)
        mode = self.collect_params()["crop_mode"]
        self.aspect_preview.set_mode(mode)
        self.mode_hint.setText(
            tr("裁剪填充：居中裁掉超出比例的边缘，铺满画布。", "Crop to fill: center-crop the edges to fill the canvas.")
            if mode == CROP_COVER else
            tr("完整留白：保持原图比例，完整保留内容，空余区域填白。", "Contain with padding: preserve the full image and fill unused space with white."))
        self.file_card.set_target_fmt(f"JPG · {size[0]}×{size[1]}")
        self._sync_output_hint()

    def _selected_source(self) -> str:
        files = self.file_card.files()
        rows = self.file_card.table.selectionModel().selectedRows()
        if rows and 0 <= rows[0].row() < len(files):
            return files[rows[0].row()]
        return files[0] if files else ""

    def _sync_preview_source(self) -> None:
        path = self._selected_source()
        self.btn_preview.setEnabled(bool(path))
        self.btn_preview.setToolTip(path or tr("请先添加图片", "Add an image first"))
        # 文件变更后清理旧图，避免将上一张的效果误认为当前选择的输出。
        if path != self._preview_path:
            self._preview_path = ""
            self.aspect_preview.set_source(QImage())
            self.preview_hint.setText(
                tr("选中图片后点击“预览裁剪”，比较两种适配效果。", "Select an image and click Preview crop to compare fit modes.")
                if path else tr("添加图片后可预览；当前仅示意输出比例。", "Add images to preview; only the output aspect ratio is shown now."))
            self.preview_hint.setToolTip("")
        elif not path:
            self.preview_hint.setText(tr("添加图片后可预览；当前仅示意输出比例。", "Add images to preview; only the output aspect ratio is shown now."))
        self._sync_output_hint()

    def _preview_crop(self) -> None:
        path = self._selected_source()
        if not path:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        # 预览只解码受限缩略图；超大图仍可批量处理，不在界面线程全量展开。
        if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > PREVIEW_MAX_PIXELS:
            error = tr("图片无法预览或超过 4000 万像素；仍可尝试批量裁剪。", "Preview unavailable or over 40 megapixels; batch cropping is still available.")
            source = QImage()
        else:
            reader.setScaledSize(size.scaled(PREVIEW_SIZE, Qt.KeepAspectRatio))
            source = reader.read()
            error = tr("无法读取预览：{}", "Cannot read preview: {}").format(reader.errorString()) if source.isNull() else ""
        if source.isNull():
            self._preview_path = ""
            self.aspect_preview.set_source(QImage())
            self.preview_hint.setText(error)
            toast.show_warning(self, error)
            return
        self._preview_path = path
        self.aspect_preview.set_source(source)
        self.preview_hint.setText(tr("预览：{}（缩略效果，最终以导出文件为准）", "Preview: {} (thumbnail; refer to the exported file for final quality)").format(os.path.basename(path)))
        self.preview_hint.setToolTip(path)

    def _sync_output_hint(self) -> None:
        size = self.collect_params()["preset_size"]
        self.output_hint.setText(
            tr("整批 {} 张 · JPG · {}×{} px；文件名附加尺寸，重名自动编号，不覆盖源文件。",
               "All {} images · JPG · {}×{} px; dimensions are added to names, duplicates are numbered, sources are preserved.").format(
                   len(self.file_card.files()), size[0], size[1]))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "params_grid"):
            self.params_grid.set_columns(
                1 if self.viewport().width() < 820 else 2)

    def _empty_hint(self):
        return tr("请先添加要裁剪的图片", "Add images to crop first")
