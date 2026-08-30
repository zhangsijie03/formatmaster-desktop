# -*- coding: utf-8 -*-
"""id_photo_panel — 证件照换底色面板。

双引擎（AI 人像抠图 / 自适应色度键兜底）+ 实时预览：
添加照片并选择底色后，可实时预览换底效果，满意再批量处理。
"""
import os
import tempfile

from PySide6.QtCore import Qt, QThread, Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QBoxLayout, QColorDialog, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, CheckBox, ComboBox, FluentIcon,
                            PrimaryPushButton, PushButton, Slider)

from core.id_photo import (PHOTO_SIZES, TRANSPARENT_KEY, is_model_ready, size_to_px,
                           CARD_PRESETS, PAPER_SIZES, draw_head_guides,
                           layout_print, check_compliance)
from gui_qt import task_manager as tm
from gui_qt.components import toast
from gui_qt.components.form_widgets import FormGrid, FormSection, CollapsibleSection
from gui_qt.components.page_header import PageHeader
from gui_qt.i18n import tr
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif"}
# 目标底色下拉（显示名 ↔ _BG_KEYS 键一一对应；偏好/任务存键，中英界面语义一致）
BG_VALUES = [tr("蓝底", "Blue"), tr("红底", "Red"), tr("白底", "White"),
             tr("灰底", "Gray"), tr("绿底", "Green"), tr("黑底", "Black"),
             tr("透明PNG", "Transparent PNG"), tr("自定义…", "Custom…")]
_BG_KEYS = ["蓝底", "红底", "白底", "灰底", "绿底", "黑底", "透明PNG", "自定义"]
# 证件照尺寸下拉项（显示名与 PHOTO_SIZES 键一一对应）
SIZE_VALUES = [tr("原尺寸", "Original"),
               tr("小1寸 (259×377)", "1-inch small (259×377)"),
               tr("1寸 (295×413)", "1-inch (295×413)"),
               tr("大1寸 (390×567)", "1-inch large (390×567)"),
               tr("小2寸 (413×531)", "2-inch small (413×531)"),
               tr("2寸 (413×579)", "2-inch (413×579)"),
               tr("大2寸 (413×626)", "2-inch large (413×626)"),
               tr("3寸 (550×840)", "3-inch (550×840)"),
               tr("美签照 (601×601)", "US Visa (601×601)"),
               tr("4寸 (898×1205)", "4-inch (898×1205)"),
               tr("5寸 (1051×1500)", "5-inch (1051×1500)"),
               tr("6寸 (1205×1795)", "6-inch (1205×1795)"),
               tr("自定义…", "Custom…")]
_SIZE_KEYS = ["原尺寸", "小1寸", "1寸", "大1寸", "小2寸", "2寸",
              "大2寸", "3寸", "美签照", "4寸", "5寸", "6寸", "自定义"]
_UNIT_KEYS = ["px", "cm", "inch"]
# 证照预设（手动 + 各类型）
PRESET_VALUES = [tr("手动设置", "Manual")] + [tr(k, k) for k in CARD_PRESETS]
_PRESET_KEYS = ["__manual__"] + list(CARD_PRESETS.keys())
# 冲印相纸
PAPER_VALUES = [tr("A6 (10.5×14.8cm)", "A6 (10.5×14.8cm)"),
                tr("6寸 (10.2×15.2cm)", "6-inch (10.2×15.2cm)"),
                tr("5寸 (8.9×12.7cm)", "5-inch (8.9×12.7cm)"),
                tr("4寸 (7.6×10.2cm)", "4-inch (7.6×10.2cm)")]
_PAPER_KEYS = ["A6", "6寸", "5寸", "4寸"]

# 预览临时输出（系统临时目录，每次覆盖；透明预览为 PNG）
_PREVIEW_OUT = os.path.join(tempfile.gettempdir(), "FormatMaster_idphoto_preview.png")
_PREVIEW_DISPLAY = os.path.join(
    tempfile.gettempdir(), "FormatMaster_idphoto_preview_display.png")


def _checker_composite(rgb, alpha):
    """棋盘格垫底合成（透明预览用，直观看到透明区域）。"""
    import numpy as _np
    h, w = alpha.shape[:2]
    yy, xx = _np.mgrid[:h, :w]
    cell = 16
    checker = _np.full((h, w, 3), 210, dtype=_np.uint8)
    checker[((yy // cell) + (xx // cell)) % 2 == 0] = 235
    a = alpha[..., None].astype(_np.float32) / 255.0
    return (rgb.astype(_np.float32) * a
            + checker.astype(_np.float32) * (1 - a)).astype(_np.uint8)


class _PreviewWorker(SafeWorker):
    """后台跑一次换底生成预览图，信号回传结果。"""

    sig_done = Signal(bool, str)  # (成功, 预览图路径)

    def __init__(self, src, out, color, use_ai, size=None, offset=0.0,
                 show_guides=False, display_out=_PREVIEW_DISPLAY, parent=None):
        super().__init__(parent)
        self._src, self._out = src, out
        self._color, self._use_ai = color, use_ai
        self._size, self._offset = size, offset
        self._show_guides = show_guides
        self._display_out = display_out

    def work(self):
        try:
            from core.id_photo import change_background, draw_head_guides
            ok = change_background(
                self._src, self._out, self._color, None, self._use_ai,
                size=self._size, offset=self._offset)
            if self.is_stopped():
                return
            display_path = self._out
            # 棋盘格和参考线只属于界面辅助层，正式预览原图留给保存/打印，
            # 防止把棋盘格或红色参考框烘焙进最终成品。
            if ok and (self._color == TRANSPARENT_KEY
                       or (self._show_guides and self._size is not None)):
                try:
                    from PIL import Image
                    with Image.open(self._out) as _im:
                        visual = _im.convert("RGBA")
                    if self._color == TRANSPARENT_KEY:
                        checker = self._checker_image(visual)
                        visual.close()
                        visual = checker
                    if self._show_guides and self._size is not None:
                        guided = draw_head_guides(visual)
                        visual.close()
                        visual = guided
                    visual.save(self._display_out)
                    visual.close()
                    display_path = self._display_out
                except Exception:  # noqa: BLE001 - 辅助层失败则显示干净原图
                    display_path = self._out
            self.sig_done.emit(bool(ok), display_path if ok else "")
        except Exception:  # noqa: BLE001 - 预览失败仅提示，不打断流程
            self.sig_done.emit(False, "")

    @staticmethod
    def _checker_image(img):
        """生成棋盘格显示副本，不修改带 alpha 的干净预览图。"""
        from PIL import Image
        import numpy as _np
        rgba = img.convert("RGBA")
        rgb_image = rgba.convert("RGB")
        rgb = _np.asarray(rgb_image)
        alpha = _np.asarray(rgba)[..., 3]
        result = Image.fromarray(_checker_composite(rgb, alpha), "RGB")
        rgb_image.close()
        rgba.close()
        return result



class _CheckWorker(SafeWorker):
    """后台跑合规自检，信号回传结果 dict（失败为 None）。"""

    sig_done = Signal(object)

    def __init__(self, path, size, offset, use_ai, parent=None):
        super().__init__(parent)
        self._path, self._size = path, size
        self._offset, self._use_ai = offset, use_ai

    def work(self):
        try:
            from core.id_photo import check_compliance
            result = check_compliance(self._path, self._size,
                                      self._offset, self._use_ai)
        except Exception:  # noqa: BLE001 - 自检失败仅提示
            result = None
        if not self.is_stopped():
            self.sig_done.emit(result)



class IdPhotoPanelPage(BaseQtPanel, TaskPanelMixin):
    """证件照换底色页。"""

    panel_key = "id_photo"
    need_ffmpeg = False

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("证件照换底色", "ID photo background"),
            tr("AI 抠出人像换任意底色或导出透明 PNG，适合证件照办理",
               "AI extracts the person; swap to any color or export a transparent PNG"),
            FluentIcon.PEOPLE)
        lay.addWidget(self.header)

        self.file_card = FileListCard(tr("照片列表", "Photos"), file_exts=PHOTO_EXTS)
        lay.addWidget(self.file_card)
        self.input_hint = CaptionLabel(tr(
            "建议使用正面、光线均匀、人物边缘清晰的照片。预览只显示当前选中的照片，开始处理会处理整批。",
            "Use a front-facing, evenly lit photo with clear subject edges. Preview shows the selected photo; Start processes the whole batch."))
        self.input_hint.setWordWrap(True)
        lay.addWidget(self.input_hint)

        card = FormSection(tr("换底设置", "Background settings"), FluentIcon.BRUSH)
        self.settings_grid = FormGrid(columns=2)
        grid = self.settings_grid

        def _combo(items, default):
            cb = ComboBox()
            cb.addItems(items)
            cb.setCurrentText(default)
            return cb

        # 主参数（直接可见）：预设 / 底色 / 尺寸
        self.cb_preset = grid.add_field(
            tr("证照预设", "Card preset"), _combo(PRESET_VALUES, PRESET_VALUES[0]),
            hint=tr("选择证件类型自动套用尺寸与底色，也可手动调整",
                    "Pick a card type to auto-apply size & color; editable"))
        self.cb_bg = grid.add_field(
            tr("目标底色", "Target color"), _combo(BG_VALUES, tr("蓝底", "Blue")),
            hint=tr("多色可选，透明 PNG 保留 alpha 可叠加到任意画面",
                    "Multiple colors; transparent PNG keeps alpha for layering"))
        self.cb_size = grid.add_field(
            tr("证件照尺寸", "Photo size"), _combo(SIZE_VALUES, SIZE_VALUES[0]),
            hint=tr("小1寸=2.2×3.2cm、1寸=2.5×3.5cm、大1寸=3.3×4.8cm、"
                    "小2寸=3.5×4.5cm、2寸=3.5×4.9cm、大2寸=3.5×5.3cm、"
                    "3寸=4.7×7.1cm、美签照=5.1×5.1cm、4寸=7.6×10.2cm、"
                    "5寸=8.9×12.7cm、6寸=10.2×15.2cm（300dpi）",
                    "1in=2.5×3.5cm, 2in=3.5×4.9cm, 3in=4.7×7.1cm, "
                    "US Visa=5.1×5.1cm, 4in=7.6×10.2cm, 5in=8.9×12.7cm, "
                    "6in=10.2×15.2cm (300dpi)"))
        card.add_form(grid)
        self.cb_ai = CheckBox(
            tr("AI 人像抠图（推荐，适合复杂背景与人物细节）",
               "AI portrait matting (recommended for complex backgrounds and fine details)"))
        self.cb_ai.setChecked(True)
        card.add_widget(self.cb_ai)
        self.engine_hint = CaptionLabel()
        self.engine_hint.setWordWrap(True)
        card.add_widget(self.engine_hint)

        # 高级设置（渐进式披露：默认折叠，进阶项）
        self.adv_collapse = CollapsibleSection(
            tr("高级设置", "Advanced"),
            hint=tr("自定义颜色/尺寸、裁剪位置、导出 DPI、命名（进阶选项）",
                    "Custom color/size, crop position, export DPI, naming (advanced)"))
        adv_grid = FormGrid(columns=1)

        # 自定义颜色（选中“自定义…”时显示）：任意选色
        color_row = QWidget()
        color_lay = QHBoxLayout(color_row)
        color_lay.setContentsMargins(0, 0, 0, 0)
        color_lay.setSpacing(8)
        self.btn_color = PushButton(tr("选择颜色…", "Pick color…"))
        self.lb_swatch = QLabel()
        self.lb_swatch.setFixedSize(28, 20)
        self.lb_swatch.setStyleSheet(
            "border:1px solid rgba(128,128,128,0.5); border-radius:3px;")
        color_lay.addWidget(self.btn_color)
        color_lay.addWidget(self.lb_swatch)
        color_lay.addStretch(1)
        self.color_row = color_row
        adv_grid.add_field(tr("自定义颜色", "Custom color"), color_row)
        color_row.setVisible(False)

        # 自定义尺寸输入（选中“自定义…”时显示）
        cust_row = QWidget()
        cust_lay = QHBoxLayout(cust_row)
        cust_lay.setContentsMargins(0, 0, 0, 0)
        cust_lay.setSpacing(8)
        self.le_cw = QLineEdit()
        self.le_cw.setPlaceholderText(tr("宽", "W"))
        self.le_cw.setFixedWidth(70)
        self.le_ch = QLineEdit()
        self.le_ch.setPlaceholderText(tr("高", "H"))
        self.le_ch.setFixedWidth(70)
        self.cb_unit = _combo([tr("像素", "px"), tr("厘米", "cm"), tr("英寸", "inch")],
                              tr("像素", "px"))
        self.cb_unit.setFixedWidth(90)
        cust_lay.addWidget(QLabel(tr("宽", "W")))
        cust_lay.addWidget(self.le_cw)
        cust_lay.addWidget(QLabel(tr("高", "H")))
        cust_lay.addWidget(self.le_ch)
        cust_lay.addWidget(QLabel(tr("单位", "Unit")))
        cust_lay.addWidget(self.cb_unit)
        cust_lay.addStretch(1)
        self.cust_row = cust_row
        adv_grid.add_field(tr("自定义尺寸", "Custom size"), cust_row)
        cust_row.setVisible(False)

        # 裁剪位置（仅选尺寸时有意义）：滑块 -20~20
        pos_row = QWidget()
        pos_lay = QHBoxLayout(pos_row)
        pos_lay.setContentsMargins(0, 0, 0, 0)
        pos_lay.setSpacing(10)
        self.sl_offset = Slider(Qt.Horizontal)
        self.sl_offset.setRange(-20, 20)
        self.sl_offset.setValue(0)
        self.sl_offset.setFixedWidth(180)
        self.lb_offset = CaptionLabel(tr("居中", "Center"))
        pos_lay.addWidget(self.sl_offset)
        pos_lay.addWidget(self.lb_offset)
        pos_lay.addStretch(1)
        adv_grid.add_field(
            tr("裁剪位置", "Crop position"), pos_row,
            hint=tr("偏上保留更多头顶，偏下保留更多下巴", "Shift up to keep more headroom, down for more chin"))

        # 导出 DPI 与 JPG 质量
        self.cb_dpi = _combo([tr("96 DPI", "96 DPI"), tr("150 DPI", "150 DPI"),
                              tr("300 DPI", "300 DPI"), tr("600 DPI", "600 DPI")],
                             tr("300 DPI", "300 DPI"))
        self.cb_quality = _combo([tr("80%", "80%"), tr("90%", "90%"),
                                  tr("95%", "95%"), tr("100%", "100%")],
                                 tr("95%", "95%"))
        dpi_row = QWidget()
        dpi_lay = QHBoxLayout(dpi_row)
        dpi_lay.setContentsMargins(0, 0, 0, 0)
        dpi_lay.setSpacing(8)
        dpi_lay.addWidget(self.cb_dpi)
        dpi_lay.addWidget(QLabel(tr("JPG 质量", "JPG quality")))
        dpi_lay.addWidget(self.cb_quality)
        dpi_lay.addStretch(1)
        adv_grid.add_field(tr("导出设置", "Export"), dpi_row)

        self.adv_collapse.add_layout(adv_grid)
        self.cb_named = CheckBox(
            tr("输出文件名添加 尺寸_底色 标记",
               "Append size_color tag to output filename"))
        self.cb_named.setChecked(False)
        self.adv_collapse.add_widget(self.cb_named)
        card.add_widget(self.adv_collapse)

        # ── 实时预览 ─────────────────────────────
        pv_card = FormSection(tr("实时预览", "Live preview"), FluentIcon.VIEW)
        self.pv_label = QLabel(tr("添加照片并选择底色，此处实时预览换底效果",
                                  "Add photos and pick a color to preview the result live"))
        self.pv_label.setAlignment(Qt.AlignCenter)
        self.pv_label.setMinimumHeight(280)
        self.pv_label.setWordWrap(True)
        self.pv_label.setStyleSheet(
            "border: 1px dashed rgba(128,128,128,0.45); border-radius: 8px;"
            "color: rgba(128,128,128,0.75); background: transparent; font-size: 13px;")
        pv_card.add_widget(self.pv_label)
        pv_head = QHBoxLayout()
        self.btn_preview = PushButton(FluentIcon.VIEW, tr("预览效果", "Preview"))
        self.btn_check = PushButton(tr("合规自检", "Compliance"))
        self.pv_status = CaptionLabel(
            tr("切换底色/AI 开关会自动刷新预览", "Switching color or AI toggles refreshes the preview"))
        pv_head.addWidget(self.btn_preview)
        pv_head.addWidget(self.btn_check)
        pv_head.addWidget(self.pv_status)
        pv_head.addStretch(1)
        pv_card.add_layout(pv_head)
        self.preview_hint = CaptionLabel(tr(
            "合规自检仅供构图参考，提交前请以办理机构的尺寸和背景要求为准。",
            "Compliance results are framing guidance only. Check the issuing authority's size and background requirements before submission."))
        self.preview_hint.setWordWrap(True)
        pv_card.add_widget(self.preview_hint)
        # 头部比例参考线（仅预览辅助）
        self.cb_guides = CheckBox(
            tr("显示头部比例参考线", "Show head-ratio guide"))
        self.cb_guides.setChecked(False)
        pv_card.add_widget(self.cb_guides)
        # 一键冲印排版
        print_row = QWidget()
        pl = QHBoxLayout(print_row)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        self.cb_print = CheckBox(tr("一键冲印排版", "Print layout"))
        self.cb_print.setChecked(False)
        self.cb_paper = _combo(PAPER_VALUES, PAPER_VALUES[0])
        self.cb_paper.setFixedWidth(180)
        pl.addWidget(self.cb_print)
        pl.addWidget(QLabel(tr("相纸", "Paper")))
        pl.addWidget(self.cb_paper)
        pl.addStretch(1)
        pv_card.add_widget(print_row)

        # 证件照是典型“调参数并看结果”的创作流程。宽屏下将设置与实时
        # 预览并排，预览不再被推到首屏以下，也显著缩短整页滚动距离。
        workspace = QWidget(self)
        workspace_lay = QHBoxLayout(workspace)
        self.workspace_lay = workspace_lay
        self.preview_card = pv_card
        workspace_lay.setContentsMargins(0, 0, 0, 0)
        workspace_lay.setSpacing(14)
        workspace_lay.addWidget(card, 3)
        pv_card.setMinimumWidth(360)
        workspace_lay.addWidget(pv_card, 2)
        lay.addWidget(workspace)

        # ── 排版打印：1寸/2寸排满一版 A6 照片纸，直接打印 ──
        self.print_collapse = CollapsibleSection(
            tr("排版打印", "Print layout"),
            hint=tr("把当前尺寸证件照排满一版 A6 照片纸（1寸一版 16 张、2寸 9 张），"
                    "预览后保存或直接打印", "Tile the photo onto an A6 sheet (16×1-inch or 9×2-inch), save or print"))
        p_row = QWidget()
        pl2 = QHBoxLayout(p_row)
        pl2.setContentsMargins(0, 0, 0, 0)
        pl2.setSpacing(8)
        pl2.addWidget(QLabel(tr("纸张", "Paper")))
        self.cb_layout_paper = _combo(PAPER_VALUES, PAPER_VALUES[0])
        self.cb_layout_paper.setFixedWidth(180)
        pl2.addWidget(self.cb_layout_paper)
        pl2.addStretch(1)
        self.print_collapse.add_widget(p_row)

        act_row = QWidget()
        ar = QHBoxLayout(act_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(8)
        self.btn_layout = PushButton(FluentIcon.VIEW,
                                     tr("生成排版预览", "Layout preview"))
        self.btn_layout.clicked.connect(self._make_layout)
        ar.addWidget(self.btn_layout)
        self.btn_layout_save = PushButton(FluentIcon.SAVE,
                                          tr("保存排版图", "Save layout"))
        self.btn_layout_save.clicked.connect(self._save_layout)
        ar.addWidget(self.btn_layout_save)
        self.btn_print = PrimaryPushButton(FluentIcon.PRINT,
                                           tr("直接打印", "Print"))
        self.btn_print.clicked.connect(self._print_layout)
        ar.addWidget(self.btn_print)
        self.lb_layout = CaptionLabel("")
        ar.addWidget(self.lb_layout)
        ar.addStretch(1)
        self.print_collapse.add_widget(act_row)

        from qfluentwidgets import ImageLabel
        self.layout_preview = ImageLabel(
            tr("先点「预览效果」生成单张照片，再点「生成排版预览」",
               "Preview first, then generate the layout"), self)
        self.layout_preview.setAlignment(Qt.AlignCenter)
        self.layout_preview.setMinimumHeight(220)
        self.layout_preview.setBorderRadius(8, 8, 8, 8)
        self.print_collapse.add_widget(self.layout_preview)
        lay.addWidget(self.print_collapse)

        out_card = FormSection(tr("输出目录", "Output folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        self.output_hint = CaptionLabel()
        self.output_hint.setWordWrap(True)
        out_card.add_widget(self.output_hint)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)

        # ── 预览联动 ─────────────────────────────
        self._pv_worker = None
        self._check_worker = None
        self._preview_pending = False
        self._preview_ready = False
        self._custom_color = (219, 64, 64)      # 自定义底色默认（红）
        self._reserved_output_paths = set()
        # 实时记忆由基类 BaseQtPanel._install_auto_prefs 统一提供（防抖自动保存）
        self.btn_preview.clicked.connect(lambda: self._preview_file())
        self.btn_check.clicked.connect(self._run_compliance)
        self.cb_bg.currentTextChanged.connect(lambda _: self._on_bg_changed())
        self.cb_ai.toggled.connect(lambda _: self._on_param_changed())
        self.cb_size.currentIndexChanged.connect(self._on_size_changed)
        self.cb_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.cb_print.toggled.connect(lambda _: self._on_param_changed())
        self.cb_paper.currentIndexChanged.connect(lambda _: self._on_param_changed())
        self.cb_guides.toggled.connect(lambda _: self._on_param_changed())
        self.cb_dpi.currentIndexChanged.connect(lambda _: self._on_param_changed())
        self.cb_quality.currentIndexChanged.connect(lambda _: self._on_param_changed())
        self.btn_color.clicked.connect(self._pick_custom_color)
        self.le_cw.textChanged.connect(lambda _: self._on_param_changed())
        self.le_ch.textChanged.connect(lambda _: self._on_param_changed())
        self.cb_unit.currentIndexChanged.connect(lambda _: self._on_param_changed())
        self.sl_offset.valueChanged.connect(self._update_offset_label)
        self.sl_offset.valueChanged.connect(lambda _: self._on_param_changed())
        self.file_card.files_changed.connect(lambda: self._preview_file())
        self.file_card.files_changed.connect(self._sync_target_summary)
        self.file_card.file_double_clicked.connect(self._preview_file)
        # 初始状态：原尺寸时裁剪位置滑块禁用、自定义行隐藏
        self._on_size_changed(self.cb_size.currentIndex())
        self._on_bg_changed()
        self._sync_target_summary()

        self.services.task_manager.register_runner(
            "id_photo", lambda task: self._runner)
        self._wire_tasks()

    # ── 参数/偏好 ────────────────────────────────
    def _on_param_changed(self):
        """任一参数变化：触发自动保存（基类防抖） + 刷新预览。"""
        self._preview_ready = False
        self._schedule_prefs_save()
        self._sync_target_summary()
        self._preview_file()

    def _bg_key(self):
        idx = self.cb_bg.currentIndex()
        key = _BG_KEYS[idx] if 0 <= idx < len(_BG_KEYS) else "蓝底"
        if key == "自定义":
            return self._custom_rgb()
        return key

    def _custom_rgb(self) -> tuple:
        r, g, b = self._custom_color
        return (int(r), int(g), int(b))

    def _set_swatch(self):
        r, g, b = self._custom_rgb()
        self.lb_swatch.setStyleSheet(
            f"background: rgb({r},{g},{b});"
            " border:1px solid rgba(128,128,128,0.5); border-radius:3px;")

    def _on_bg_changed(self):
        idx = self.cb_bg.currentIndex()
        key = _BG_KEYS[idx] if 0 <= idx < len(_BG_KEYS) else "蓝底"
        self.color_row.setVisible(key == "自定义")
        if key == "自定义":
            self.adv_collapse.set_expanded(True)  # 需输入颜色，自动展开高级区
        self._set_swatch()
        self._preview_ready = False
        self._schedule_prefs_save()
        self._sync_target_summary()
        self._preview_file()

    def _pick_custom_color(self):
        r, g, b = self._custom_rgb()
        c = QColorDialog.getColor(
            QColor(r, g, b), self, tr("选择自定义底色", "Pick custom color"))
        if c.isValid():
            self._custom_color = (c.red(), c.green(), c.blue())
            self._set_swatch()
            self._schedule_prefs_save()
            self._preview_file()

    def collect_params(self) -> dict:
        custom = None
        if self._size_key() == "自定义":
            unit = _UNIT_KEYS[self.cb_unit.currentIndex()]
            custom = size_to_px(self.le_cw.text(), self.le_ch.text(), unit)
        preset_index = self.cb_preset.currentIndex()
        return {
            "bg": self._bg_key(),
            "use_ai": self.cb_ai.isChecked(),
            "named": self.cb_named.isChecked(),
            "size_key": self._size_key(),
            "custom_size": custom,
            "custom_width": self.le_cw.text(),
            "custom_height": self.le_ch.text(),
            "custom_unit": _UNIT_KEYS[self.cb_unit.currentIndex()],
            "preset_key": (_PRESET_KEYS[preset_index]
                           if 0 <= preset_index < len(_PRESET_KEYS)
                           else "__manual__"),
            "offset": self.sl_offset.value() / 100.0,
            "show_guides": self.cb_guides.isChecked(),
            "do_print": self.cb_print.isChecked(),
            "paper": _PAPER_KEYS[self.cb_paper.currentIndex()],
            "layout_paper": _PAPER_KEYS[self.cb_layout_paper.currentIndex()],
            "dpi": int(self.cb_dpi.currentText().split()[0]),
            "quality": int(self.cb_quality.currentText().rstrip("%")),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def collect_prefs(self) -> dict:
        return self.collect_params()

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("preset_key") in _PRESET_KEYS:
            self.cb_preset.setCurrentIndex(
                _PRESET_KEYS.index(prefs["preset_key"]))
        bg = prefs.get("bg")
        if isinstance(bg, (tuple, list)) and len(bg) == 3:
            try:
                custom_color = tuple(int(c) for c in bg)
                if any(c < 0 or c > 255 for c in custom_color):
                    raise ValueError
                self._custom_color = custom_color
                self.cb_bg.setCurrentIndex(_BG_KEYS.index("自定义"))
            except (TypeError, ValueError, OverflowError):
                pass  # 损坏偏好不覆盖安全默认色
        elif bg in _BG_KEYS:
            self.cb_bg.setCurrentIndex(_BG_KEYS.index(str(bg)))
        if "use_ai" in prefs:
            self.cb_ai.setChecked(bool(prefs["use_ai"]))
        if "named" in prefs:
            self.cb_named.setChecked(bool(prefs["named"]))
        if prefs.get("size_key") in _SIZE_KEYS:
            self.cb_size.setCurrentIndex(_SIZE_KEYS.index(str(prefs["size_key"])))
        if "offset" in prefs:
            try:
                self.sl_offset.setValue(int(round(float(prefs["offset"]) * 100)))
            except (TypeError, ValueError):  # noqa: BLE001 - 配置损坏时跳过
                pass
        if "show_guides" in prefs:
            self.cb_guides.setChecked(bool(prefs["show_guides"]))
        if "do_print" in prefs:
            self.cb_print.setChecked(bool(prefs["do_print"]))
        if prefs.get("paper") in _PAPER_KEYS:
            self.cb_paper.setCurrentIndex(_PAPER_KEYS.index(prefs["paper"]))
        if prefs.get("layout_paper") in _PAPER_KEYS:
            self.cb_layout_paper.setCurrentIndex(
                _PAPER_KEYS.index(prefs["layout_paper"]))
        if "dpi" in prefs:
            for i, t in enumerate(["96 DPI", "150 DPI", "300 DPI", "600 DPI"]):
                if t.startswith(str(prefs["dpi"])):
                    self.cb_dpi.setCurrentIndex(i)
                    break
        if "quality" in prefs:
            idx = self.cb_quality.findText(str(prefs["quality"]) + "%")
            if idx >= 0:
                self.cb_quality.setCurrentIndex(idx)
        if prefs.get("custom_unit") in _UNIT_KEYS:
            self.cb_unit.setCurrentIndex(
                _UNIT_KEYS.index(prefs["custom_unit"]))
        if prefs.get("custom_width") is not None:
            self.le_cw.setText(str(prefs["custom_width"]))
        if prefs.get("custom_height") is not None:
            self.le_ch.setText(str(prefs["custom_height"]))
        elif prefs.get("custom_size"):
            try:
                cw, ch = prefs["custom_size"]
                self.le_cw.setText(str(int(cw)))
                self.le_ch.setText(str(int(ch)))
            except Exception:  # noqa: BLE001
                pass
        # 应用尺寸/底色后同步自定义行可见性
        self._on_size_changed(self.cb_size.currentIndex())
        self._on_bg_changed()
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 实时预览 ─────────────────────────────────
    def _size_key(self) -> str:
        idx = self.cb_size.currentIndex()
        return _SIZE_KEYS[idx] if 0 <= idx < len(_SIZE_KEYS) else "原尺寸"

    def _update_offset_label(self, v: int):
        if v > 0:
            self.lb_offset.setText(tr(f"偏上 {v}%", f"Up {v}%"))
        elif v < 0:
            self.lb_offset.setText(tr(f"偏下 {-v}%", f"Down {-v}%"))
        else:
            self.lb_offset.setText(tr("居中", "Center"))

    def _on_size_changed(self, idx: int):
        is_custom = 0 <= idx < len(_SIZE_KEYS) and _SIZE_KEYS[idx] == "自定义"
        self.cust_row.setVisible(is_custom)
        if is_custom:
            self.adv_collapse.set_expanded(True)  # 需输入尺寸，自动展开高级区
        self.sl_offset.setEnabled(idx != 0)
        self._preview_ready = False
        self._schedule_prefs_save()
        self._sync_target_summary()
        self._preview_file()

    def _on_preset_changed(self, idx: int):
        if 0 <= idx < len(_PRESET_KEYS):
            key = _PRESET_KEYS[idx]
            if key in CARD_PRESETS:
                p = CARD_PRESETS[key]
                if p["size"] in _SIZE_KEYS:
                    self.cb_size.setCurrentIndex(_SIZE_KEYS.index(p["size"]))
                if p["bg"] in _BG_KEYS:
                    self.cb_bg.setCurrentIndex(_BG_KEYS.index(p["bg"]))
                self._on_size_changed(self.cb_size.currentIndex())
        self._schedule_prefs_save()
        self._preview_file()

    def _preview_file(self, path=None):
        """预览指定文件；缺省取当前选中行，再取列表第一张。"""
        self._preview_ready = False
        if self._pv_worker and self._pv_worker.isRunning():
            # 参数快速连改时记住刷新请求；当前线程结束后只重跑最新状态，
            # 避免界面最终停留在旧底色或旧尺寸的预览上。
            self._preview_pending = True
            return
        if not path:
            files = self.file_card.files()
            if not files:
                self._pv_status_text(tr("请先添加照片", "Add photos first"))
                return
            rows = sorted({i.row() for i in self.file_card.table.selectedIndexes()})
            path = files[rows[0]] if rows and rows[0] < len(files) else files[0]
        if not os.path.isfile(path):
            return
        self.btn_preview.setEnabled(False)
        self._pv_status_text(tr("预览中…", "Previewing…"))
        size = PHOTO_SIZES.get(self._size_key())
        if size is None and self._size_key() == "自定义":
            unit = _UNIT_KEYS[self.cb_unit.currentIndex()]
            size = size_to_px(self.le_cw.text(), self.le_ch.text(), unit)
        self._pv_worker = _PreviewWorker(
            path, _PREVIEW_OUT,
            self._bg_key(), self.cb_ai.isChecked(),
            size=size, offset=self.sl_offset.value() / 100.0,
            show_guides=self.cb_guides.isChecked())
        self._pv_worker.sig_done.connect(self._on_preview_done)
        self._pv_worker.finished.connect(self._on_preview_finished)
        self._pv_worker.start()

    def _on_preview_done(self, ok, path):
        if self._preview_pending:
            return  # 最新参数即将补跑，不闪现已经过期的中间结果
        self._preview_ready = bool(ok and path and os.path.isfile(path))
        if ok and path and os.path.isfile(path):
            pix = QPixmap(path)
            if not pix.isNull():
                w = max(self.pv_label.width() - 24, 120)
                h = max(self.pv_label.height() - 24, 120)
                self.pv_label.setPixmap(
                    pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self.pv_label.setStyleSheet(
                    "border: 1px solid rgba(128,128,128,0.25); border-radius: 8px;"
                    "background: transparent;")
                self._pv_status_text(tr("预览完成", "Preview ready"))
                return
        self.pv_label.clear()
        self.pv_label.setText(tr("预览失败：图片无法处理", "Preview failed: cannot process image"))
        self.pv_label.setStyleSheet(
            "border: 1px dashed rgba(128,128,128,0.45); border-radius: 8px;"
            "color: rgba(128,128,128,0.75); background: transparent; font-size: 13px;")
        self._pv_status_text("")

    def _on_preview_finished(self):
        self.btn_preview.setEnabled(True)
        if self._preview_pending:
            self._preview_pending = False
            self._preview_file()

    def _pv_status_text(self, text):
        self.pv_status.setText(text)

    # ── 合规自检 ─────────────────────────────────
    def _run_compliance(self):
        files = self.file_card.files()
        if not files:
            self._pv_status_text(tr("请先添加照片", "Add photos first"))
            return
        rows = sorted({i.row() for i in self.file_card.table.selectedIndexes()})
        path = files[rows[0]] if rows and rows[0] < len(files) else files[0]
        if not os.path.isfile(path):
            return
        size = PHOTO_SIZES.get(self._size_key())
        if size is None and self._size_key() == "自定义":
            unit = _UNIT_KEYS[self.cb_unit.currentIndex()]
            size = size_to_px(self.le_cw.text(), self.le_ch.text(), unit)
        self.btn_check.setEnabled(False)
        self._pv_status_text(tr("合规自检中…", "Checking…"))
        self._check_worker = _CheckWorker(
            path, size, self.sl_offset.value() / 100.0, self.cb_ai.isChecked())
        self._check_worker.sig_done.connect(self._on_compliance_done)
        self._check_worker.start()

    def _on_compliance_done(self, result):
        self.btn_check.setEnabled(True)
        if not result:
            self._pv_status_text(tr("自检失败：无法处理该图片", "Check failed"))
            return
        lines = [
            tr("头部距上边 {}%", "Head top {}%").format(result["head_top_pct"]),
            tr("人像高度占 {}%", "Person height {}%").format(result["person_h_pct"]),
            tr("人像宽度占 {}%", "Person width {}%").format(result["person_w_pct"]),
        ]
        if result["ok"]:
            msg = (tr("✅ 合规：头部位置与人像占比符合常见证件照标准",
                      "Compliant: head position & person ratio meet common standards")
                   + "\n" + "\n".join(lines))
        else:
            msg = (tr("⚠️ 建议调整：", "Needs adjustment")
                   + "\n" + "\n".join(result["issues"]) + "\n\n"
                   + "\n".join(lines))
        QMessageBox.information(self, tr("合规自检", "Compliance"), msg)

    # ── 排版打印（A6 一版多张 + 直接打印）────────
    def _make_layout(self):
        """用当前预览单张 → layout_print 排满所选相纸，返回 PIL Image。"""
        if not self._preview_ready or not os.path.isfile(_PREVIEW_OUT):
            self.lb_layout.setText(
                tr("请先点击「预览效果」生成单张照片", "Run Preview first"))
            return None
        from PIL import Image
        paper_key = _PAPER_KEYS[self.cb_layout_paper.currentIndex()]
        paper = PAPER_SIZES.get(paper_key, PAPER_SIZES["A6"])
        try:
            with Image.open(_PREVIEW_OUT) as _src:
                single = _src.convert("RGB")
            img = layout_print(single, paper, dpi=300)
        except Exception as e:  # noqa: BLE001
            self.lb_layout.setText(tr("排版失败：{}", "Layout failed: {}").format(e))
            return None
        self._layout_img = img
        tmp = os.path.join(tempfile.gettempdir(),
                           "FormatMaster_idphoto_layout.png")
        try:
            img.save(tmp)
            pix = QPixmap(tmp)
            if not pix.isNull():
                self.layout_preview.setPixmap(pix.scaled(
                    380, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:  # noqa: BLE001
            pass
        cols = max(1, img.width // max(1, single.width))
        rows = max(1, img.height // max(1, single.height))
        self.lb_layout.setText(
            tr("{} · 一版排 {} 张", "{} · {} per sheet")
            .format(paper_key, cols * rows))
        return img

    def _save_layout(self):
        img = self._make_layout()
        if img is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("保存排版图", "Save layout"),
            f"证件照排版_{self._layout_paper_name()}.png", "PNG (*.png)")
        if path:
            try:
                img.save(path, dpi=(300, 300))
            except Exception as e:  # noqa: BLE001
                toast.show_error(self, tr("保存失败：{}", "Save failed: {}").format(e))
                return
            toast.show_success(self, tr("已保存：{}", "Saved: {}").format(path))

    def _layout_paper_name(self):
        return _PAPER_KEYS[self.cb_layout_paper.currentIndex()]

    def _print_layout(self):
        img = self._make_layout()
        if img is None:
            return
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        from PySide6.QtWidgets import QDialog
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle(tr("打印证件照排版", "Print ID photo layout"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tmp = os.path.join(tempfile.gettempdir(),
                           "FormatMaster_idphoto_print.png")
        try:
            img.save(tmp)
            qimg = QImage(tmp)
            painter = QPainter(printer)
            rect = painter.viewport()
            size = qimg.size()
            size.scale(rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            painter.setViewport(rect.x(), rect.y(), size.width(), size.height())
            painter.setWindow(qimg.rect())
            painter.drawImage(0, 0, qimg)
            painter.end()
            toast.show_success(self, tr("已发送到打印机", "Sent to printer"))
        except Exception as e:  # noqa: BLE001
            toast.show_error(self, tr("打印失败：{}", "Print failed: {}").format(e))

    # ── 任务执行器 ───────────────────────────────
    def _runner(self, task, prog):
        from core.id_photo import change_background
        from PIL import Image
        params = task.params
        bg = params.get("bg", "蓝底")
        offset = params.get("offset", 0.0)
        dpi = int(params.get("dpi", 300) or 300)
        quality = int(params.get("quality", 95) or 95)
        size = PHOTO_SIZES.get(params.get("size_key", "原尺寸"))
        if size is None and params.get("size_key") == "自定义":
            size = params.get("custom_size")
        if not params.get("do_print"):
            return change_background(
                task.file_path, task.output_path, bg, prog,
                use_ai=params.get("use_ai", True), size=size, offset=offset,
                dpi=dpi, quality=quality)

        # 冲印模式先生成临时单张，再原子替换最终排版图；排版失败或取消时
        # 不能把半成品单张冒充成功结果，也不能破坏已有同名文件。
        output_dir = os.path.dirname(os.path.abspath(task.output_path))
        os.makedirs(output_dir, exist_ok=True)
        suffix = os.path.splitext(task.output_path)[1] or ".jpg"
        fd, single_path = tempfile.mkstemp(
            prefix=".fm_idphoto_single_", suffix=suffix, dir=output_dir)
        os.close(fd)
        os.remove(single_path)
        staged_path = ""
        try:
            def _background_progress(pct, message):
                prog(pct if pct < 0 else min(80, round(pct * 0.8)), message)

            if not change_background(
                    task.file_path, single_path, bg, _background_progress,
                    use_ai=params.get("use_ai", True), size=size, offset=offset,
                    dpi=dpi, quality=quality):
                return False
            prog(88, tr("生成冲印排版…", "Creating print layout…"))
            with Image.open(single_path) as source:
                single = source.convert("RGB")
            paper = PAPER_SIZES.get(params.get("paper", "6寸"),
                                    PAPER_SIZES["6寸"])
            layout = layout_print(single, paper, dpi=dpi)
            single.close()
            fd, staged_path = tempfile.mkstemp(
                prefix=".fm_idphoto_layout_", suffix=suffix, dir=output_dir)
            os.close(fd)
            os.remove(staged_path)
            save_kwargs = {"dpi": (dpi, dpi)}
            if suffix.lower() in (".jpg", ".jpeg"):
                save_kwargs["quality"] = quality
            layout.save(staged_path, **save_kwargs)
            layout.close()
            prog(98, tr("写入排版结果…", "Saving print layout…"))
            os.replace(staged_path, task.output_path)
            return True
        except InterruptedError:
            raise
        except (OSError, ValueError) as exc:
            prog(-1, tr("排版失败：{}", "Layout failed: {}").format(exc))
            return False
        finally:
            for path in (single_path, staged_path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass

    def _make_task(self, f):
        params = self.collect_params()
        out_dir = self.out_row.resolve_dir(f)
        ext = ".png" if params["bg"] == TRANSPARENT_KEY else ".jpg"
        nm = None
        if params.get("named"):
            size_tag = params.get("size_key", "原尺寸")
            bg = params["bg"]
            if isinstance(bg, (tuple, list)):
                r, g, b = (int(x) for x in bg)
                bg_tag = f"RGB{r}{g}{b}"
            elif bg == TRANSPARENT_KEY:
                bg_tag = "透明"
            else:
                bg_tag = bg
            nm = os.path.splitext(os.path.basename(f))[0] + f"_{size_tag}_{bg_tag}"
        out_path = tm.make_output_path(f, out_dir, ext, name=nm)
        base, output_ext = os.path.splitext(out_path)
        counter = 1
        normalized = os.path.normcase(os.path.abspath(out_path))
        while normalized in self._reserved_output_paths:
            out_path = f"{base}_{counter}{output_ext}"
            normalized = os.path.normcase(os.path.abspath(out_path))
            counter += 1
        self._reserved_output_paths.add(normalized)
        bg = params["bg"]
        if isinstance(bg, (tuple, list)):
            r, g, b = (int(x) for x in bg)
            target_label = f"自定义 ({r},{g},{b})"
        else:
            target_label = bg
        return dict(
            name=f"{tr('证件照换底色', 'ID photo')} - {os.path.basename(f)}",
            task_type="id_photo", file_path=f, output_path=out_path,
            params=params, runner=self._runner, runner_key="id_photo",
            history_type=tr("证件照换底色", "ID photo"), history_target=target_label,
            need_ffmpeg=False)

    def _start(self):
        params = self.collect_params()
        if params["size_key"] == "自定义" and params["custom_size"] is None:
            toast.show_warning(
                self, tr("请输入有效的自定义宽度和高度", "Enter a valid custom width and height"))
            return False
        self._reserved_output_paths = {
            os.path.normcase(os.path.abspath(task.output_path))
            for task_id in self._task_rows
            if (task := self.services.task_manager.get_task(task_id)) is not None
        }
        return self._submit_files()

    def _empty_hint(self):
        return tr("请先添加要处理的照片文件", "Add photos to process first")

    def _sync_target_summary(self):
        """让文件列表与主按钮明确显示当前将生成什么结果。"""
        summary = tr("{} / {}", "{} / {}").format(
            self.cb_size.currentText(), self.cb_bg.currentText())
        if self.cb_print.isChecked():
            summary = tr("{} 冲印排版 / {}", "{} print layout / {}").format(
                self.cb_paper.currentText(), summary)
        self.file_card.set_target_fmt(summary)
        self.action_bar.btn_go.setText(
            tr("生成冲印排版", "Create print layout")
            if self.cb_print.isChecked()
            else tr("开始换底", "Change background"))
        if self.cb_ai.isChecked():
            self.engine_hint.setText(
                tr("AI 模型已就绪。处理时优先使用 AI，异常时自动回退到色度键算法。",
                   "The AI model is ready. Processing prefers AI and falls back to chroma key if needed.")
                if is_model_ready() else
                tr("首次使用会下载约 26 MB 的 AI 模型；下载失败或不可用时会自动回退到色度键算法。",
                   "First use downloads an AI model of about 26 MB. If unavailable, processing falls back to chroma key."))
        else:
            self.engine_hint.setText(tr(
                "当前使用色度键算法，适合纯色或接近纯色的背景；复杂背景建议开启 AI。",
                "Chroma key is active. It works best on plain or near-solid backgrounds; enable AI for complex scenes."))

        count = len(self.file_card.files())
        output_kind = (tr("透明 PNG", "transparent PNG")
                       if self._bg_key() == TRANSPARENT_KEY else
                       tr("JPG", "JPG"))
        if self.cb_print.isChecked():
            result = tr("每张照片生成 1 张 {} 相纸排版图（{}）。",
                        "Each photo creates one {} sheet layout ({}).").format(
                            self.cb_paper.currentText(), output_kind)
        else:
            result = tr("每张照片生成 1 张{}证件照。",
                        "Each photo creates one {} ID photo.").format(output_kind)
        naming = (tr("文件名会附加尺寸和底色标记。",
                     "Filenames include size and color tags.")
                  if self.cb_named.isChecked() else
                  tr("输出沿用原文件名。", "Output keeps the source filename."))
        self.output_hint.setText(tr(
            "当前批次 {count} 张。{result}{naming}重名处理沿用全局设置，源文件保持不变。",
            "Batch: {count}. {result} {naming} Name conflicts follow global settings; source files stay unchanged.").format(
                count=count, result=result, naming=naming))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.viewport().width() < 900
        grid = getattr(self, "settings_grid", None)
        if grid is not None:
            grid.set_columns(1 if narrow else 2)
        workspace = getattr(self, "workspace_lay", None)
        if workspace is not None:
            workspace.setDirection(
                QBoxLayout.Direction.TopToBottom if narrow
                else QBoxLayout.Direction.LeftToRight)
            self.preview_card.setMinimumWidth(0 if narrow else 360)

    def closeEvent(self, event):
        # 页面销毁后禁止后台预览/自检继续回调 UI；耗时核心步骤完成后线程
        # 会看到停止标志并自然退出，避免阻塞窗口关闭。
        for worker in (getattr(self, "_pv_worker", None),
                       getattr(self, "_check_worker", None)):
            if worker is not None and worker.isRunning():
                worker.stop()
        super().closeEvent(event)
