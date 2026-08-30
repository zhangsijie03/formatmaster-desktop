"""compress_img_panel — 图片压缩面板（阶段2 迁移自 gui/panels/compress_img_panel.py）。

批量压缩图片体积，保持格式不变，支持限制最大分辨率。
任务经 TaskManager 通用链路执行 core.tools.image_compress（不依赖 FFmpeg）。
"""
import os

from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon

from core.tools import image_compress
from gui_qt import task_manager as tm
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

# 预置值（与 tkinter 版 compress_img_panel 一致）
QUALITY_VALUES = ["95", "85", "75", "60", "50", "40", "30"]
SIZE_VALUES = [tr("不限制", "No limit"), "1920x1080", "1280x720", "800x600"]
SIZE_LIMITS = [None, (1920, 1080), (1280, 720), (800, 600)]
TARGET_KB_VALUES = ["100", "200", "500", "1024", "2048", "5120"]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


class CompressImgPanelPage(BaseQtPanel, TaskPanelMixin):
    """图片压缩页。"""

    panel_key = "image_compress"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("图片压缩", "Image Compress"),
            tr("批量减小图片体积，保持文件格式与透明通道，可选限制最大分辨率",
               "Reduce image sizes in batch while preserving format and transparency"),
            FluentIcon.ZIP_FOLDER)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=IMAGE_EXTS)
        lay.addWidget(self.file_card)

        sec = FormSection(tr("压缩设置", "Compress settings"), FluentIcon.ZIP_FOLDER)

        # 压缩模式属于参数上下文，收进同一卡片，避免悬在卡片之间。
        from qfluentwidgets import SegmentedWidget
        self.seg_mode = SegmentedWidget(self)
        self.seg_mode.addItem("quality", tr("按质量压缩", "By quality"))
        self.seg_mode.addItem("target", tr("按目标大小", "By target size"))
        self.seg_mode.setCurrentItem("quality")
        self.seg_mode.currentItemChanged.connect(self._on_mode_changed)
        self.seg_mode.setFixedHeight(36)
        sec.add_widget(self.seg_mode)

        self.params_grid = FormGrid(columns=2)

        self.cb_q = self.params_grid.add_field(
            tr("输出质量", "Output quality"), self._combo(QUALITY_VALUES, "75"),
            hint=tr("JPEG/WebP 数值越低体积越小；PNG/TIFF 使用无损压缩",
                    "Lower means smaller JPEG/WebP; PNG/TIFF remain lossless"))
        self.cb_sz = self.params_grid.add_field(
            tr("最大分辨率", "Max resolution"), self._combo(SIZE_VALUES, tr("不限制", "No limit")),
            hint=tr("限制输出图片的最大分辨率", "Limit max output resolution"))
        sec.add_form(self.params_grid)

        self._mode = "quality"  # 当前模式（currentItemChanged 信号携带 key）
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        self._target_row = QWidget()
        tr_lay = QVBoxLayout(self._target_row)
        tr_lay.setContentsMargins(0, 0, 0, 0)
        self.target_grid = FormGrid(columns=1)
        self.cb_target = self._combo(TARGET_KB_VALUES, "500")
        self.target_grid.add_field(
            tr("目标上限 (KB)", "Target limit (KB)"), self.cb_target,
            hint=tr("程序会先降低质量，再按需缩小分辨率；无法达到时明确报错",
                    "Quality is reduced first, then resolution; failure is reported honestly"))
        tr_lay.addLayout(self.target_grid)
        sec.add_widget(self._target_row)
        self._target_row.setVisible(False)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)
        lay.addWidget(sec)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始压缩", "Compress"))
        lay.addWidget(self.action_bar)

        self._reserved_output_paths = set()
        self.services.task_manager.register_runner(
            "compress_img", lambda task: self._runner)
        self._wire_tasks()
        self.file_card.files_changed.connect(self._sync_target_summary)
        self.cb_q.currentTextChanged.connect(self._sync_target_summary)
        self.cb_sz.currentIndexChanged.connect(self._sync_target_summary)
        self.cb_target.currentTextChanged.connect(self._sync_target_summary)
        self._sync_target_summary()

    def _combo(self, items, default):
        cb = ComboBox()
        cb.addItems(items)
        cb.setCurrentText(default)
        return cb

    def _on_mode_changed(self, key: str):
        """模式切换：target=目标大小（展开 KB 行+置灰质量/分辨率），quality=恢复。"""
        if key not in ("quality", "target"):
            return
        self._mode = key
        on = key == "target"
        self._target_row.setVisible(on)
        for w in (self.cb_q, self.cb_sz):
            w.setEnabled(not on)
        self.action_bar.btn_go.setText(
            tr("压缩至目标大小", "Compress to target")
            if on else tr("开始压缩", "Compress"))
        self._sync_target_summary()

    def _target_kb(self):
        if self._mode != "target":
            return None
        try:
            return int(self.cb_target.currentText())
        except (TypeError, ValueError, OverflowError):
            return None

    def _max_size(self):
        index = self.cb_sz.currentIndex()
        return SIZE_LIMITS[index] if 0 <= index < len(SIZE_LIMITS) else None

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "mode": self._mode,
            "quality": int(self.cb_q.currentText()),
            "max_size": self._max_size(),
            "target_kb": self._target_kb(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "quality": self.cb_q.currentText(),
            "size": self.cb_sz.currentText(),
            "size_index": self.cb_sz.currentIndex(),
            "mode": self._mode,
            "target_kb": self._target_kb(),
            "target_on": self._mode == "target",
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("quality") in QUALITY_VALUES:
            self.cb_q.setCurrentText(prefs["quality"])
        size_index = prefs.get("size_index")
        if isinstance(size_index, int) and 0 <= size_index < len(SIZE_VALUES):
            self.cb_sz.setCurrentIndex(size_index)
        elif prefs.get("size") in SIZE_VALUES:
            self.cb_sz.setCurrentText(prefs["size"])
        if str(prefs.get("target_kb")) in TARGET_KB_VALUES:
            self.cb_target.setCurrentText(str(prefs["target_kb"]))
        mode = prefs.get("mode")
        if mode not in ("quality", "target"):
            mode = "target" if prefs.get("target_on") else "quality"
        self.seg_mode.setCurrentItem(mode)
        self._on_mode_changed(mode)
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        tkb = p.get("target_kb")
        if tkb:
            from core.tools import image_compress_to_size
            ok, msg, _size = image_compress_to_size(
                task.file_path, task.output_path, int(tkb), prog)
            if not ok:
                task.error = msg or tr("压缩失败", "Compress failed")
                return False
            return True
        q = p.get("quality", 75)
        max_sz = p.get("max_size")
        # 兼容旧任务快照中按界面文案保存的 size 参数。
        if max_sz is None:
            sz_str = p.get("size", tr("不限制", "No limit"))
            if isinstance(sz_str, str) and "x" in sz_str:
                try:
                    max_sz = tuple(int(value) for value in sz_str.split("x", 1))
                except ValueError:
                    max_sz = None
        return image_compress(task.file_path, task.output_path,
                              q, max_sz, prog)

    def _make_task(self, f):
        params = self.collect_params()
        nm = os.path.splitext(os.path.basename(f))[0]
        ext = os.path.splitext(f)[1].lower()
        out_dir = self.out_row.resolve_dir(f)
        # 统一走 make_output_path：应用冲突策略（自动改名/覆盖）+ 源目同路径保护
        out_path = tm.make_output_path(f, out_dir, ext, name=nm + "_compressed")
        base, output_ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{output_ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        return dict(
            name=f"{tr('图片压缩', 'Image Compress')} - {os.path.basename(f)}",
            task_type="compress_img", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="compress_img",
            history_type=tr("图片压缩", "Image Compress"),
            history_target=(
                tr("至 {}KB", "to {}KB").format(params.get("target_kb"))
                if params.get("target_kb") else
                tr("质量 {}", "Quality {}").format(params["quality"])),
            need_ffmpeg=False)

    def _start(self):
        if self._mode == "target" and self._target_kb() is None:
            from gui_qt.components import toast
            toast.show_warning(self, tr("请选择有效的目标大小", "Choose a valid target size"))
            return False
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要压缩的图片", "Add images to compress first")

    def _sync_target_summary(self, *_args):
        if self._mode == "target":
            target_kb = self.cb_target.currentText()
            target = tr("原格式 · ≤ {} KB", "Source · ≤ {} KB").format(
                target_kb)
            self.mode_hint.setText(tr(
                "每张图片独立压到不超过 {target} KB：JPEG/WebP 先逐步降低质量，仍超限时再等比缩小分辨率；PNG/BMP/TIFF 直接按需缩小。目标过小时可能明显降低分辨率，确实无法达到会报错。",
                "Each image is compressed independently to at most {target} KB. JPEG/WebP reduce quality first, then scale down if needed; PNG/BMP/TIFF scale down as needed. Very small targets may greatly reduce resolution, and unreachable targets report an error.").format(target=target_kb))
        else:
            size = self.cb_sz.currentText()
            quality = self.cb_q.currentText()
            target = tr("原格式 · 质量 {} · {}", "Source · Quality {} · {}").format(
                quality, size)
            size_detail = (tr("不限制分辨率", "resolution is not limited")
                           if self.cb_sz.currentIndex() == 0 else
                           tr("仅将超出尺寸的图片等比缩小到 {} 以内，不会放大小图",
                              "only images above the limit are scaled proportionally within {}; smaller images are never enlarged").format(size))
            self.mode_hint.setText(tr(
                "JPEG/WebP 使用质量 {quality}；PNG/TIFF 使用无损压缩，BMP 的体积通常变化有限。{size_detail}，并保持原文件格式及该格式支持的透明通道。",
                "JPEG/WebP use quality {quality}; PNG/TIFF use lossless compression, while BMP size may change little. The {size_detail}. Source format and supported transparency are preserved.").format(
                    quality=quality, size_detail=size_detail))
        self.file_card.set_target_fmt(target)
        self.output_hint.setText(tr(
            "整批 {count} 张图片，各生成“文件名_compressed.原扩展名”；重名沿用全局冲突设置，不修改源文件。压缩后的文件不保证一定小于原文件。",
            "Batch: {count} images. Each produces filename_compressed.<source extension>; name conflicts follow global settings. Source files stay unchanged. Processed files are not guaranteed to be smaller.").format(count=len(self.file_card.files())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grid = getattr(self, "params_grid", None)
        if grid is not None:
            grid.set_columns(1 if self.viewport().width() < 820 else 2)
