"""image_panel — 图片转换面板（阶段2 迁移自 gui/panels/image_panel.py）。

JPG · PNG · BMP · GIF · TIFF · WEBP · ICO 格式互转，支持质量/缩放/旋转/
裁剪/灰度/文字水印。任务经 TaskManager 通用链路执行 core.image_converter
（PIL 实现，不依赖 FFmpeg）。
"""
import os
import re
from functools import partial

from qfluentwidgets import (CaptionLabel, CheckBox, ComboBox, DoubleSpinBox,
                            FluentIcon, LineEdit)

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow
from utils.config import SUPPORTED_IMAGE

# 预置值（与 tkinter 版 image_panel 一致）
QUALITY_VALUES = [tr("100（最高质量）", "100 (highest)"), tr("95（高质量）", "95 (high)"), tr("85（中等）", "85 (medium)"), tr("70（低质量）", "70 (low)"), tr("50（压缩）", "50 (small)")]
# 与图片引擎实际使用 quality 的输出格式一致；其余格式不能靠该参数调质。
QUALITY_FORMATS = frozenset({"JPG", "WEBP", "AVIF", "HEIC"})
LEGACY_MAX_QUALITY_VALUES = frozenset({"100（无损）", "100 (lossless)"})
SIZE_VALUES = [tr("原始大小", "Original size"), "50%", "25%", "200%"]
ROTATE_VALUES = ["0°", "90°", "180°", "270°"]
CROP_VALUES = [tr("原始比例", "Original ratio"), tr("裁剪为正方形", "Crop to square")]
WATERMARK_POS_VALUES = [tr("右下角", "Bottom right"), tr("左下角", "Bottom left"), tr("右上角", "Top right"), tr("左上角", "Top left"), tr("居中", "Center")]
EFFECT_VALUES = [tr("无特效", "None"), tr("水平翻转", "Flip horizontal"),
                 tr("垂直翻转", "Flip vertical"), tr("反色", "Invert"),
                 tr("浮雕", "Emboss"), tr("边缘检测", "Edges"),
                 tr("锐化", "Sharpen")]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff",
              ".webp", ".ico", ".tga", ".heic", ".heif"}


class ImagePanelPage(BaseQtPanel, TaskPanelMixin):
    """图片转换页。"""

    panel_key = "image"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("图片转换", "Image Conversion"),
            tr("选择输出格式与缩放，需要时再调整裁剪、水印和特效",
               "Choose format and scale; optionally crop, watermark or apply effects"),
            FluentIcon.PHOTO))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=IMAGE_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt("PNG")

        lay.addWidget(self._build_params_card())

        from gui_qt.components.form_widgets import FormSection
        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始转换", "Convert"))
        lay.addWidget(self.action_bar)

        self.cb_fmt.currentTextChanged.connect(self.file_card.set_target_fmt)
        self.cb_fmt.currentTextChanged.connect(self._sync_quality_controls)
        self._sync_quality_controls()
        self._wire_tasks()

    def _build_params_card(self):
        from gui_qt.components.form_widgets import (FormSection, FormGrid,
                                                    CollapsibleSection)

        sec = FormSection(tr("转换参数", "Convert settings"), FluentIcon.SETTING)

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        # 主参数（直接可见）
        self.main_grid = FormGrid(columns=3)
        self.cb_fmt = self.main_grid.add_field(
            tr("目标格式", "Target format"), _combo(list(SUPPORTED_IMAGE), "PNG"),
            hint=tr("输出图片格式", "Output image format"))
        self.cb_q = self.main_grid.add_field(
            tr("质量", "Quality"), _combo(QUALITY_VALUES, tr("95（高质量）", "95 (high)")),
            hint=tr("数值越高通常保留更多细节；100 不等于无损", "Higher values usually retain more detail; 100 does not mean lossless"))
        self.cb_sz = self.main_grid.add_field(
            tr("缩放", "Scale"), _combo(SIZE_VALUES, tr("原始大小", "Original size")))
        sec.add_form(self.main_grid)
        self.quality_hint = CaptionLabel()
        self.quality_hint.setWordWrap(True)
        sec.add_widget(self.quality_hint)

        # 高级设置（渐进式披露：默认折叠）
        adv = CollapsibleSection(
            tr("高级设置", "Advanced"),
            hint=tr("裁剪、水印、特效与隐私", "Crop, watermark, effects and privacy"))
        self.adv_grid = FormGrid(columns=2)
        self.cb_rotate = self.adv_grid.add_field(
            tr("旋转", "Rotate"), _combo(ROTATE_VALUES, "0°"))
        self.cb_crop = self.adv_grid.add_field(
            tr("裁剪", "Crop"), _combo(CROP_VALUES, tr("原始比例", "Original ratio")))
        self.wm_edit = self.adv_grid.add_field(
            tr("水印文字", "Watermark text"), LineEdit(), colspan=1,
            hint=tr("留空则不添加水印", "blank = no watermark"))
        self.wm_edit.setPlaceholderText(tr("留空则不添加水印", "blank = no watermark"))
        self.cb_wm_pos = self.adv_grid.add_field(
            tr("水印位置", "Watermark position"), _combo(WATERMARK_POS_VALUES, tr("右下角", "Bottom right")))
        # 先填写文字再选择位置；留空时保留位置值，但禁用无效操作。
        self.cb_wm_pos.setEnabled(False)
        self.wm_edit.textChanged.connect(
            lambda text: self.cb_wm_pos.setEnabled(bool(text.strip())))
        adv.add_layout(self.adv_grid)

        self.cb_gray = CheckBox(tr("转为黑白（灰度）", "Convert to grayscale"))
        adv.add_widget(self.cb_gray)
        self.cb_strip = CheckBox(tr("清除 EXIF 隐私信息（拍摄设备/GPS）", "Strip EXIF privacy data (camera/GPS)"))
        adv.add_widget(self.cb_strip)

        # 增强与特效（2026-08-15 合并）
        adv.add_widget(CaptionLabel(tr("增强与特效", "Enhance & effects")))
        self.fx_grid = FormGrid(columns=2)
        self.sp_contrast = DoubleSpinBox()
        self.sp_contrast.setRange(0.5, 2.0); self.sp_contrast.setSingleStep(0.1); self.sp_contrast.setValue(1.0)
        self.sp_saturation = DoubleSpinBox()
        self.sp_saturation.setRange(0.0, 3.0); self.sp_saturation.setSingleStep(0.1); self.sp_saturation.setValue(1.0)
        self.sp_sharpness = DoubleSpinBox()
        self.sp_sharpness.setRange(0.0, 5.0); self.sp_sharpness.setSingleStep(0.1); self.sp_sharpness.setValue(1.0)
        self.fx_grid.add_field(tr("对比度", "Contrast"), self.sp_contrast,
                          hint=tr("1.0 为原样，>1 增强", "1.0 = original, >1 = stronger"))
        self.fx_grid.add_field(tr("饱和度", "Saturation"), self.sp_saturation,
                          hint=tr("1.0 为原样，0 为黑白", "1.0 = original, 0 = grayscale"))
        self.fx_grid.add_field(tr("锐度", "Sharpness"), self.sp_sharpness,
                          hint=tr("1.0 为原样，>1 更锐利", "1.0 = original, >1 = sharper"))
        self.cb_effect = self.fx_grid.add_field(
            tr("特效", "Effect"), _combo(EFFECT_VALUES, tr("无特效", "None")),
            hint=tr("翻转 / 反色 / 浮雕 / 边缘检测 / 锐化", "Flip / invert / emboss / edges / sharpen"))
        adv.add_layout(self.fx_grid)

        sec.add_widget(adv)
        return sec

    def _sync_quality_controls(self, *_args) -> None:
        """切换格式只更新适用状态，不清空质量选择，方便切回有损格式。"""
        fmt = self.cb_fmt.currentText()
        adjustable = fmt in QUALITY_FORMATS
        self.cb_q.setEnabled(adjustable)
        hint = (tr("质量越高通常保留更多细节；100 也不代表无损编码。",
                   "Higher quality usually retains more detail; 100 is not lossless encoding.")
                if adjustable else
                tr("{} 不使用质量参数；仍可调整缩放和高级设置。",
                   "{} does not use the quality setting; scale and advanced options still apply.").format(fmt))
        self.quality_hint.setText(hint)
        self.cb_q.setToolTip(hint)

    # ── 参数/偏好 ────────────────────────────────
    def collect_params(self) -> dict:
        return {
            "fmt": self.cb_fmt.currentText(),
            "quality": self.cb_q.currentText(),
            "size": self.cb_sz.currentText(),
            "watermark": self.wm_edit.text().strip(),
            "watermark_pos": self.cb_wm_pos.currentText(),
            "rotate": self.cb_rotate.currentText(),
            "crop": self.cb_crop.currentText(),
            "grayscale": self.cb_gray.isChecked(),
            "strip_exif": self.cb_strip.isChecked(),
            "contrast": self.sp_contrast.value(),
            "saturation": self.sp_saturation.value(),
            "sharpness": self.sp_sharpness.value(),
            "effect": self.cb_effect.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return {
            "fmt": self.cb_fmt.currentText(),
            "quality": self.cb_q.currentText(),
            "size": self.cb_sz.currentText(),
            "rotate": self.cb_rotate.currentText(),
            "crop": self.cb_crop.currentText(),
            "grayscale": self.cb_gray.isChecked(),
            "strip_exif": self.cb_strip.isChecked(),
            "contrast": self.sp_contrast.value(),
            "saturation": self.sp_saturation.value(),
            "sharpness": self.sp_sharpness.value(),
            "effect": self.cb_effect.currentText(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("fmt") in SUPPORTED_IMAGE:
            self.cb_fmt.setCurrentText(prefs["fmt"])
        # 旧版“100（无损）”只是误导性文案，恢复为同一个数值 100，
        # 不因修正文案而把用户已保存的最高质量降回默认值。
        if prefs.get("quality") in LEGACY_MAX_QUALITY_VALUES:
            self.cb_q.setCurrentText(QUALITY_VALUES[0])
        elif prefs.get("quality") in QUALITY_VALUES:
            self.cb_q.setCurrentText(prefs["quality"])
        if prefs.get("size") in SIZE_VALUES:
            self.cb_sz.setCurrentText(prefs["size"])
        if prefs.get("rotate") in ROTATE_VALUES:
            self.cb_rotate.setCurrentText(prefs["rotate"])
        if prefs.get("crop") in CROP_VALUES:
            self.cb_crop.setCurrentText(prefs["crop"])
        if "grayscale" in prefs:
            self.cb_gray.setChecked(bool(prefs["grayscale"]))
        if "strip_exif" in prefs:
            self.cb_strip.setChecked(bool(prefs["strip_exif"]))
        # 高级图像参数同样属于用户工作流偏好，返回页面时应完整恢复。
        for key, control in (("contrast", self.sp_contrast),
                             ("saturation", self.sp_saturation),
                             ("sharpness", self.sp_sharpness)):
            try:
                value = float(prefs.get(key, control.value()))
            except (TypeError, ValueError):
                continue
            if control.minimum() <= value <= control.maximum():
                control.setValue(value)
        if prefs.get("effect") in EFFECT_VALUES:
            self.cb_effect.setCurrentText(prefs["effect"])
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog, converter=None):
        p = task.params
        # 显示文本会随语言变化，只读取开头数值，避免英文 “95 (high)”
        # 被直接 int() 后导致整批图片任务失败。
        quality_text = str(p.get(
            "quality", tr("95（高质量）", "95 (high)")))
        quality_match = re.match(r"\s*(\d{1,3})", quality_text)
        quality = int(quality_match.group(1)) if quality_match else 95
        resize_factor = {"50%": 0.5, "25%": 0.25, "200%": 2.0}.get(
            p.get("size", tr("原始大小", "Original size")), 1.0)
        rotate_val = int(p.get("rotate", "0°").replace("°", ""))
        return (converter or self.services.image_conv).convert(
            task.file_path, task.output_path, quality, None,
            p.get("watermark", ""),
            self._resolve_watermark_position(p.get("watermark_pos", "")),
            rotate=rotate_val,
            crop_mode=self._resolve_crop_mode(p.get("crop", "")),
            grayscale=p.get("grayscale", False),
            strip_exif=p.get("strip_exif", False),
            resize_factor=resize_factor, progress_callback=prog,
            contrast=p.get("contrast", 1.0),
            saturation=p.get("saturation", 1.0),
            sharpness=p.get("sharpness", 1.0),
            effect=self._resolve_effect(p.get("effect", "")))

    @staticmethod
    def _resolve_effect(text):
        """特效中文名 → core 特效 key。"""
        mapping = {tr("水平翻转", "Flip horizontal"): "hflip",
                   tr("垂直翻转", "Flip vertical"): "vflip",
                   tr("反色", "Invert"): "invert",
                   tr("浮雕", "Emboss"): "emboss",
                   tr("边缘检测", "Edges"): "edges",
                   tr("锐化", "Sharpen"): "sharpen"}
        return mapping.get(text or "", "")

    @staticmethod
    def _resolve_crop_mode(text):
        """把本地化显示值转换为图片引擎使用的稳定值。"""
        if text in (tr("裁剪为正方形", "Crop to square"), "Crop to square"):
            return "裁剪为正方形"
        return "原始比例"

    @staticmethod
    def _resolve_watermark_position(text):
        """把中英文水印位置统一为图片引擎的稳定坐标标识。"""
        mapping = {
            tr("右下角", "Bottom right"): "右下角",
            tr("左下角", "Bottom left"): "左下角",
            tr("右上角", "Top right"): "右上角",
            tr("左上角", "Top left"): "左上角",
            tr("居中", "Center"): "居中",
            "Bottom right": "右下角",
            "Bottom left": "左下角",
            "Top right": "右上角",
            "Top left": "左上角",
            "Center": "居中",
        }
        return mapping.get(text or "", "右下角")

    def _make_task(self, f):
        from core.image_converter import ImageConverter

        # 单任务取消不能污染其他并行图片转换的实例状态。
        converter = ImageConverter()
        params = self.collect_params()
        fmt_ext = SUPPORTED_IMAGE[params["fmt"]]
        out_dir = self.out_row.resolve_dir(f)
        out_path = tm.make_output_path(f, out_dir, fmt_ext)
        return dict(
            name=f"{tr('图片转换', 'Image Convert')} - {os.path.basename(f)}",
            task_type="image", file_path=f, output_path=out_path,
            params=params, runner=partial(self._runner, converter=converter),
            canceller=converter.cancel,
            history_type=tr("图片转换", "Image Convert"), history_target=params["fmt"],
            need_ffmpeg=False)

    def _start(self):
        self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要转换的图片文件", "Add images to convert first")

    def resizeEvent(self, event):
        """窄窗口将参数表单降为单列，避免控件挤压和横向截断。"""
        super().resizeEvent(event)
        width = self.viewport().width()
        main_grid = getattr(self, "main_grid", None)
        if main_grid is not None:
            main_grid.set_columns(1 if width < 820 else (2 if width < 1180 else 3))
        columns = 1 if width < 820 else 2
        for name in ("adv_grid", "fx_grid"):
            grid = getattr(self, name, None)
            if grid is not None:
                grid.set_columns(columns)
