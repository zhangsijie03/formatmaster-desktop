"""watermark_panel — 图片水印面板（阶段2 迁移自 gui/panels/watermark_panel.py）。

批量给图片添加文字或图片水印，支持透明度、旋转、位置。
任务经 TaskManager 通用链路执行 core.watermark_tool.process_watermark
（PIL 实现，不依赖 FFmpeg）。
"""
import os

from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QHBoxLayout,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (FluentIcon, CaptionLabel, ComboBox, LineEdit,
                            PushButton, RadioButton)

from core.watermark_tool import process_watermark
from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

# 预置值（与 tkinter 版 watermark_panel 一致）
FONT_SIZES = ["16", "24", "32", "48", "64", "96", "128"]
COLORS = ["#FFFFFF", "#000000", "#FF0000", "#00FF00", "#0000FF",
          "#FFFF00", "#FF00FF", "#00FFFF", "#CCCCCC", "#666666",
          "#FF6600", "#990099"]
OPACITIES = ["0.1", "0.2", "0.3", "0.5", "0.7", "0.8", "0.9", "1.0"]
ROTATIONS = ["0", "15", "30", "45", "60", "90", "180", "270", "315"]
POSITIONS = [tr("左上角", "Top left"), tr("右上角", "Top right"), tr("左下角", "Bottom left"), tr("右下角", "Bottom right"), tr("居中", "Center")]
POSITION_KEYS = ["top_left", "top_right", "bottom_left", "bottom_right", "center"]
POSITION_ALIASES = {
    "左上角": "top_left", "Top left": "top_left",
    "右上角": "top_right", "Top right": "top_right",
    "左下角": "bottom_left", "Bottom left": "bottom_left",
    "右下角": "bottom_right", "Bottom right": "bottom_right",
    "居中": "center", "Center": "center",
}
SCALES = ["0.05", "0.1", "0.15", "0.2", "0.3", "0.5", "0.8", "1.0"]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


class WatermarkPanelPage(BaseQtPanel, TaskPanelMixin):
    """图片水印页。"""

    panel_key = "watermark"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("水印处理", "Watermark Tools"),
            tr("批量添加文字或图片水印，支持透明度、旋转和精确位置",
               "Add text or image watermarks in batch with opacity, rotation and positioning"),
            FluentIcon.PENCIL_INK)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=IMAGE_EXTS)
        lay.addWidget(self.file_card)

        lay.addWidget(self._build_settings_card())

        # 输出目录
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始加水印", "Add watermark"))
        lay.addWidget(self.action_bar)

        self._reserved_output_paths = set()
        self.services.task_manager.register_runner(
            "watermark", lambda task: self._runner)
        self.rb_text.setChecked(True)
        self._mode_changed()
        self._wire_tasks()
        for combo in (self.cb_font_size, self.cb_color, self.cb_opacity,
                      self.cb_rotation, self.cb_position, self.cb_scale,
                      self.cb_opacity_img, self.cb_rotation_img,
                      self.cb_position_img):
            combo.currentTextChanged.connect(self._sync_target_summary)
        self.ed_text.textChanged.connect(self._sync_target_summary)
        self.ed_wm_path.textChanged.connect(self._sync_target_summary)
        self.file_card.files_changed.connect(self._sync_target_summary)

    def _build_settings_card(self):
        sec = FormSection(tr("水印设置", "Watermark settings"), FluentIcon.PENCIL_INK)

        # 水印类型切换
        type_row = QHBoxLayout()
        type_row.setSpacing(16)
        type_row.addWidget(CaptionLabel(tr("水印类型", "Watermark type")))
        self.rb_text = RadioButton(tr("文字水印", "Text watermark"))
        self.rb_image = RadioButton(tr("图片水印", "Image Watermark"))
        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        self.type_group.addButton(self.rb_text)
        self.type_group.addButton(self.rb_image)
        self.rb_text.toggled.connect(self._mode_changed)
        type_row.addWidget(self.rb_text)
        type_row.addWidget(self.rb_image)
        type_row.addStretch(1)
        sec.add_layout(type_row)

        self.sec_text = self._build_text_section()
        self.sec_image = self._build_image_section()
        sec.add_widget(self.sec_text)
        sec.add_widget(self.sec_image)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)
        return sec

    def _build_text_section(self):
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        self.text_grid = FormGrid(columns=3)
        self.ed_text = LineEdit()
        self.ed_text.setText(tr("水印", "Watermark"))
        self.text_grid.add_field(
            tr("水印文字", "Watermark text"), self.ed_text, colspan=3)
        self.cb_font_size = self._combo(FONT_SIZES, "48")
        self.cb_color = self._combo(COLORS, "#FFFFFF")
        self.cb_opacity = self._combo(OPACITIES, "0.8")
        self.cb_rotation = self._combo(ROTATIONS, "0")
        self.cb_position = self._combo(POSITIONS, tr("右下角", "Bottom right"))
        self.text_grid.add_field(tr("字号", "Font size"), self.cb_font_size)
        self.text_grid.add_field(tr("颜色", "Color"), self.cb_color)
        self.text_grid.add_field(tr("透明度", "Opacity"), self.cb_opacity)
        self.text_grid.add_field(tr("旋转角度", "Rotation"), self.cb_rotation)
        self.text_grid.add_field(tr("位置", "Position"), self.cb_position)
        vl.addLayout(self.text_grid)
        return w

    def _build_image_section(self):
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(8)

        self.image_grid = FormGrid(columns=3)
        self.ed_wm_path = LineEdit()
        self.ed_wm_path.setPlaceholderText(tr("未选择", "Not selected"))
        self.ed_wm_path.setReadOnly(True)
        self.btn_pick = PushButton(tr("选择", "Pick"))
        self.btn_pick.clicked.connect(self._pick_wm_image)
        path_widget = QWidget()
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        path_layout.addWidget(self.ed_wm_path, 1)
        path_layout.addWidget(self.btn_pick)
        self.image_grid.add_field(
            tr("水印图片", "Watermark image"), path_widget, colspan=3)
        self.cb_scale = self._combo(SCALES, "0.2")
        self.cb_opacity_img = self._combo(OPACITIES, "0.8")
        self.cb_rotation_img = self._combo(ROTATIONS, "0")
        self.cb_position_img = self._combo(
            POSITIONS, tr("右下角", "Bottom right"))
        self.image_grid.add_field(tr("缩放比例", "Scale"), self.cb_scale)
        self.image_grid.add_field(tr("透明度", "Opacity"), self.cb_opacity_img)
        self.image_grid.add_field(tr("旋转角度", "Rotation"), self.cb_rotation_img)
        self.image_grid.add_field(tr("位置", "Position"), self.cb_position_img)
        vl.addLayout(self.image_grid)
        return w

    def _combo(self, items, default):
        combo = ComboBox()
        combo.addItems(items)
        combo.setCurrentText(default)
        return combo

    def _mode_changed(self, *_args):
        is_text = self.rb_text.isChecked()
        self.sec_text.setVisible(is_text)
        self.sec_image.setVisible(not is_text)
        self.action_bar.btn_go.setText(
            tr("添加文字水印", "Add text watermark") if is_text
            else tr("添加图片水印", "Add image watermark"))
        self._sync_target_summary()

    def _pick_wm_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择水印图片", "Pick watermark image"), "",
            tr("PNG图片 (*.png);;所有图片 (*.png *.jpg *.bmp);;所有文件 (*)", "PNG images (*.png);;All images (*.png *.jpg *.bmp);;All files (*)"))
        if path:
            self.ed_wm_path.setText(path)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        is_text = self.rb_text.isChecked()
        position_combo = self.cb_position if is_text else self.cb_position_img
        return {
            "wm_type": "text" if is_text else "image",
            "text": self.ed_text.text(),
            "font_size": int(self.cb_font_size.currentText()),
            "color": self.cb_color.currentText(),
            "opacity": float(self.cb_opacity.currentText() if is_text
                             else self.cb_opacity_img.currentText()),
            "rotation": int(self.cb_rotation.currentText() if is_text
                            else self.cb_rotation_img.currentText()),
            "position": POSITION_KEYS[position_combo.currentIndex()],
            "wm_image_path": self.ed_wm_path.text().strip(),
            "scale": float(self.cb_scale.currentText()),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "wm_type": "text" if self.rb_text.isChecked() else "image",
            "text": self.ed_text.text(),
            "font_size": self.cb_font_size.currentText(),
            "color": self.cb_color.currentText(),
            "opacity": self.cb_opacity.currentText(),
            "rotation": self.cb_rotation.currentText(),
            "position": POSITION_KEYS[self.cb_position.currentIndex()],
            "scale": self.cb_scale.currentText(),
            "opacity_img": self.cb_opacity_img.currentText(),
            "rotation_img": self.cb_rotation_img.currentText(),
            "position_img": POSITION_KEYS[self.cb_position_img.currentIndex()],
            "wm_image_path": self.ed_wm_path.text().strip(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("wm_type") == "image":
            self.rb_image.setChecked(True)
        elif prefs.get("wm_type") == "text":
            self.rb_text.setChecked(True)
        self._mode_changed()
        if prefs.get("text"):
            self.ed_text.setText(prefs["text"])
        if prefs.get("font_size") in FONT_SIZES:
            self.cb_font_size.setCurrentText(prefs["font_size"])
        if prefs.get("color") in COLORS:
            self.cb_color.setCurrentText(prefs["color"])
        if prefs.get("opacity") in OPACITIES:
            self.cb_opacity.setCurrentText(prefs["opacity"])
        if prefs.get("rotation") in ROTATIONS:
            self.cb_rotation.setCurrentText(prefs["rotation"])
        self._apply_position_pref(self.cb_position, prefs.get("position"))
        if prefs.get("scale") in SCALES:
            self.cb_scale.setCurrentText(prefs["scale"])
        if prefs.get("opacity_img") in OPACITIES:
            self.cb_opacity_img.setCurrentText(prefs["opacity_img"])
        if prefs.get("rotation_img") in ROTATIONS:
            self.cb_rotation_img.setCurrentText(prefs["rotation_img"])
        self._apply_position_pref(
            self.cb_position_img, prefs.get("position_img", prefs.get("position")))
        wm_image_path = prefs.get("wm_image_path")
        if isinstance(wm_image_path, str):
            self.ed_wm_path.setText(wm_image_path)
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    @staticmethod
    def _apply_position_pref(combo, value):
        if value in POSITION_KEYS:
            combo.setCurrentIndex(POSITION_KEYS.index(value))
        elif value in POSITIONS:
            combo.setCurrentText(value)
        elif value in POSITION_ALIASES:
            combo.setCurrentIndex(POSITION_KEYS.index(POSITION_ALIASES[value]))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        p = task.params
        return process_watermark(
            task.file_path, task.output_path,
            wm_type=p.get("wm_type", "text"),
            text=p.get("text", ""),
            font_size=p.get("font_size", 48),
            color=p.get("color", "#FFFFFF"),
            opacity=p.get("opacity", 0.8),
            rotation=p.get("rotation", 0),
            position=p.get("position", "bottom_right"),
            wm_image_path=p.get("wm_image_path", ""),
            scale=p.get("scale", 0.2),
            progress_cb=prog)

    def _make_task(self, f):
        params = self.collect_params()
        nm = os.path.splitext(os.path.basename(f))[0]
        ext = os.path.splitext(f)[1].lower()
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(
            f, out_dir, ext, name=nm + "_watermark")
        base, output_ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{output_ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        target = (tr("文字水印", "Text watermark")
                  if params["wm_type"] == "text"
                  else tr("图片水印", "Image watermark"))
        return dict(
            name=f"{tr('图片水印', 'Image Watermark')} - {os.path.basename(f)}",
            task_type="watermark", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="watermark",
            history_type=tr("图片水印", "Image Watermark"), history_target=target,
            need_ffmpeg=False)

    def _start(self):
        params = self.collect_params()
        if params["wm_type"] == "text" and not params["text"].strip():
            toast.show_warning(
                self, tr("水印文字不能为空", "Watermark text cannot be empty"))
            return False
        if params["wm_type"] == "image":
            if not params["wm_image_path"]:
                toast.show_warning(
                    self, tr("请先选择水印图片", "Pick a watermark image first"))
                return False
            if not os.path.isfile(params["wm_image_path"]):
                toast.show_error(
                    self, tr("水印图片不存在", "Watermark image does not exist"))
                return False
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要加水印的图片", "Add images to watermark first")

    def _sync_target_summary(self, *_args):
        if self.rb_text.isChecked():
            text = self.ed_text.text().strip()
            preview = text if len(text) <= 12 else text[:12] + "…"
            opacity = round(float(self.cb_opacity.currentText()) * 100)
            rotation = int(self.cb_rotation.currentText())
            target = tr("原格式 · “{text}” · {opacity}% · {position}",
                        "Source · “{text}” · {opacity}% · {position}").format(
                            text=preview or tr("空文字", "Empty text"),
                            opacity=opacity,
                            position=self.cb_position.currentText())
            rotation_detail = (tr("，旋转 {}°", ", rotated {}°").format(rotation)
                               if rotation else "")
            self.mode_hint.setText(tr(
                "文字水印使用 {size}px 字号、颜色 {color}、{opacity}% 不透明度{rotation}，放在{position}。字号是固定像素，同一设置在高分辨率图片上会显得更小；整批图片共用这些参数。",
                "Text watermark uses {size}px, color {color}, {opacity}% opacity{rotation}, positioned at {position}. Font size is fixed in pixels, so the same setting appears smaller on higher-resolution images; the batch shares these parameters.").format(
                    size=self.cb_font_size.currentText(),
                    color=self.cb_color.currentText(), opacity=opacity,
                    rotation=rotation_detail,
                    position=self.cb_position.currentText()))
        else:
            path = self.ed_wm_path.text().strip()
            name = os.path.basename(path) if path else tr("未选择", "Not selected")
            scale = round(float(self.cb_scale.currentText()) * 100)
            opacity = round(float(self.cb_opacity_img.currentText()) * 100)
            rotation = int(self.cb_rotation_img.currentText())
            target = tr("原格式 · 图片 {scale}% · {opacity}% · {position}",
                        "Source · Image {scale}% · {opacity}% · {position}").format(
                            scale=scale, opacity=opacity,
                            position=self.cb_position_img.currentText())
            rotation_detail = (tr("，旋转 {}°", ", rotated {}°").format(rotation)
                               if rotation else "")
            self.mode_hint.setText(tr(
                "水印图片：{name}；宽度设为每张原图宽度的 {scale}%、{opacity}% 不透明度{rotation}，放在{position}。建议使用透明 PNG；原图尺寸不同，水印像素尺寸会随之变化。",
                "Watermark image: {name}; width is {scale}% of each source image, at {opacity}% opacity{rotation}, positioned at {position}. A transparent PNG is recommended; pixel size varies with each source image.").format(
                    name=name, scale=scale, opacity=opacity,
                    rotation=rotation_detail,
                    position=self.cb_position_img.currentText()))
            self.ed_wm_path.setToolTip(path)
        self.file_card.set_target_fmt(target)
        self.output_hint.setText(tr(
            "整批 {count} 张图片，各生成“文件名_watermark.原扩展名”；重名沿用全局冲突设置，不修改源文件。PNG/WebP/TIFF 可保留透明通道，JPG/BMP 的透明区域会以白色合成。",
            "Batch: {count} images. Each produces filename_watermark.<source extension>; name conflicts follow global settings. Source files stay unchanged. PNG/WebP/TIFF can preserve transparency; transparent areas are composited on white for JPG/BMP.").format(count=len(self.file_card.files())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 1 if self.viewport().width() < 820 else 3
        for grid in (getattr(self, "text_grid", None),
                     getattr(self, "image_grid", None)):
            if grid is not None:
                grid.set_columns(columns)
