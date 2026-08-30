"""video_edit_panel — 视频处理面板（剪辑 / 合并 / 字幕烧录 / 变速）。

基于 core/video_tools（FFmpeg），模式用 SegmentedWidget 切换，
复用 TaskPanelMixin 的文件列表 + 任务队列联动。
"""
import os

from PySide6.QtCore import Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, LineEdit,
                            PushButton, SegmentedWidget)

from gui_qt import task_manager as tm
from gui_qt.i18n import tr
from gui_qt.components.form_widgets import (CollapsibleSection, FormGrid,
                                             FormSection)
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel
from gui_qt.panels.task_mixin import TaskPanelMixin
from gui_qt.widgets import ActionBar, FileListCard, OutputDirRow

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
              ".m4v", ".mpg", ".mpeg", ".ts"}

MODES = [
    ("clip", tr("剪辑片段", "Clip")),
    ("merge", tr("合并视频", "Merge videos")),
    ("subtitle", tr("字幕烧录", "Burn subtitle")),
    ("speed", tr("变速处理", "Change Speed")),
    ("delogo", tr("去水印", "Remove logo")),
]
# 效果与音轨组：是否启用由 sg_category 决定；不再通过一个容易误解的
# “无特效”选项覆盖基础处理选择。
MODES2 = [
    ("reverse", tr("倒放", "Reverse")),
    ("gif", tr("转GIF", "To GIF")),
    ("watermark", tr("文字水印", "Text watermark")),
    ("stabilize", tr("视频稳定", "Stabilize")),
    ("track", tr("音轨替换", "Audio track")),
]

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
MODE_HINTS = {
    "clip": tr("起止时间应用于整批视频；剪辑器以列表首个视频预览。", "The time range applies to every video; the clip editor previews the first file."),
    "merge": tr("至少添加 2 个视频，按列表顺序合并；可在合并预览器中调整顺序。", "Add at least 2 videos; merge in list order or reorder in the merge preview."),
    "subtitle": tr("同一份字幕应用于每个视频，并固定写入画面；字幕编辑器仅支持 SRT。", "The same subtitles are burned into every video; the subtitle editor supports SRT only."),
    "speed": tr("小于 1x 为慢放，大于 1x 为加速；运动补偿仅用于慢放。", "Below 1x slows down; above 1x speeds up. Interpolation is available for slow motion only."),
    "delogo": tr("按像素指定水印区域；同一坐标应用于整批视频，请确认分辨率一致。", "Set the logo region in pixels; the same coordinates apply to all videos, so check their resolutions."),
    "reverse": tr("逐个倒放视频；可选择是否同步倒放音轨。", "Reverse each video and optionally reverse its audio too."),
    "gif": tr("每个视频导出一个无声 GIF；降低帧率或宽度可减小体积。", "Export each video as a silent GIF; lower FPS or width reduces file size."),
    "watermark": tr("同一段文字水印应用于整批视频，位置相对各视频画面计算。", "Apply the same text watermark to every video, positioned relative to its frame."),
    "stabilize": tr("逐个修正手持拍摄抖动，处理可能耗时较长。", "Reduce handheld shake in each video; processing may take longer."),
    "track": tr("替换会移除原声；混音会保留原声并叠加所选音频，背景音量仅用于混音。", "Replace removes original audio; mix keeps it and adds the selected audio. Background volume applies to mixing only."),
}


class _InfoWorker(SafeWorker):
    """后台读取视频信息（时长/分辨率），避免阻塞 UI 线程。"""

    sig_done = Signal(str, object)  # (路径, info dict)

    def __init__(self, video_path, parent=None):
        super().__init__(parent)
        self._fp = video_path

    def work(self):
        from core.ffmpeg_executor import get_ffprobe_info
        try:
            info = get_ffprobe_info(self._fp)
        except Exception:  # noqa: BLE001 - 读取失败显示占位
            info = None
        self.sig_done.emit(self._fp, info)



def _parse_time(s):
    """把 'HH:MM:SS' / 'MM:SS' 或纯秒数转秒；非法/空返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) == 1:
            value = float(parts[0])
            return value if value >= 0 else None
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), float(parts[1])
            if minutes < 0 or not 0 <= seconds < 60:
                return None
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = (
                int(parts[0]), int(parts[1]), float(parts[2]))
            if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                return None
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None
    return None


def _fmt_duration(seconds):
    """秒 → 'HH:MM:SS'（小时为 0 时省略）。"""
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return (f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}")


def _to_seconds(v):
    """把 ffprobe 的 duration（秒 或 'HH:MM:SS' 时间码）转成秒；失败返回 None。"""
    if v is None:
        return None
    try:
        s = str(v).strip()
        if ":" in s:
            secs = 0.0
            for part in s.split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(s)
    except (TypeError, ValueError):
        return None


class VideoToolsPanelPage(BaseQtPanel, TaskPanelMixin):
    """视频处理页。"""

    panel_key = "video_tools"

    # ── UI 构建 ──────────────────────────────────
    def build(self):
        lay = self.content_layout
        lay.addWidget(PageHeader(
            tr("视频处理", "Video tools"),
            tr("剪辑、合并、字幕、变速与画面效果，所有处理均在本地完成",
               "Clip, merge, subtitle, retime, and enhance video locally"),
            FluentIcon.SCROLL))

        self.file_card = FileListCard(tr("文件列表", "Files"), file_exts=VIDEO_EXTS)
        lay.addWidget(self.file_card)
        self.file_card.set_target_fmt(tr("视频处理", "Video Tools"))

        lay.addWidget(self._build_params_card())

        # 视频信息：导入文件后显示时长与分辨率
        info_wrap = QWidget()
        info_wrap.setObjectName("videoSourceSummary")
        info_row = QHBoxLayout(info_wrap)
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(8)
        self.lb_video_info = CaptionLabel(
            tr("导入视频后显示时长与分辨率", "Video duration & resolution will show here"))
        self.lb_video_info.setProperty("sec", True)
        self.lb_video_info.setWordWrap(True)
        self.lb_video_info.setStyleSheet("font-size: 12px;")
        info_row.addWidget(self.lb_video_info, 1)
        # 文件元数据属于输入上下文，收进文件卡而不是悬在两个卡片之间。
        self.file_card.layout().addWidget(info_wrap)
        self._info_workers = []
        self.file_card.files_changed.connect(self._refresh_info)

        out_card = FormSection(tr("输出目录", "Output Folder"), FluentIcon.FOLDER)
        self.out_row = OutputDirRow()
        self.out_row.bind_file_list(self.file_card)
        out_card.add_widget(self.out_row)
        lay.addWidget(out_card)

        self.action_bar = ActionBar(tr("开始处理", "Start"))
        lay.addWidget(self.action_bar)

        # 注册 runner 工厂：持久化恢复的任务可重建执行器（按 params["mode"] 分发）
        self.services.task_manager.register_runner(
            "video_tools", lambda task: self._runner)
        self._wire_tasks()
        self.file_card.files_changed.connect(self._sync_context_controls)
        self.cb_speed.currentTextChanged.connect(self._sync_context_controls)
        self.cb_track_mode.currentIndexChanged.connect(self._sync_context_controls)
        self.ed_sub.textChanged.connect(self._sync_context_controls)
        self._sync_context_controls()

    def _build_params_card(self):
        sec = FormSection(tr("处理设置", "Settings"), FluentIcon.SETTING)

        # 先选处理类别，再显示该类别下唯一的一组操作。旧版同时展示两组
        # 模式，第二组会静默覆盖第一组选择，用户难以判断实际执行内容。
        category_grid = FormGrid(columns=1)
        self.sg_category = SegmentedWidget()
        self.sg_category.addItem("basic", tr("基础处理", "Essentials"))
        self.sg_category.addItem("effects", tr("效果与音轨", "Effects & audio"))
        self.sg_category.setCurrentItem("basic")
        self.sg_category.setAccessibleName(tr("处理类别", "Processing category"))
        self.sg_category.currentItemChanged.connect(
            lambda _key: self._mode_changed())
        category_grid.add_field(
            tr("处理类别", "Category"), self.sg_category,
            hint=tr("每次仅执行当前类别中选中的一种处理",
                    "Runs one selected operation at a time"))
        sec.add_form(category_grid)

        self.w_basic_modes = QWidget()
        basic_layout = QVBoxLayout(self.w_basic_modes)
        basic_layout.setContentsMargins(0, 0, 0, 0)
        self.sg_mode = SegmentedWidget()
        for key, label in MODES:
            self.sg_mode.addItem(key, label)
        self.sg_mode.setCurrentItem("clip")
        self.sg_mode.setAccessibleName(tr("基础处理方式", "Essential operation"))
        self.sg_mode.currentItemChanged.connect(
            lambda _k: self._mode_changed())
        basic_layout.addWidget(self.sg_mode)
        sec.add_widget(self.w_basic_modes)

        self.w_effect_modes = QWidget()
        effect_layout = QVBoxLayout(self.w_effect_modes)
        effect_layout.setContentsMargins(0, 0, 0, 0)
        self.sg_mode2 = SegmentedWidget()
        for key, label in MODES2:
            self.sg_mode2.addItem(key, label)
        self.sg_mode2.setCurrentItem("reverse")
        self.sg_mode2.setAccessibleName(tr("效果处理方式", "Effect operation"))
        self.sg_mode2.currentItemChanged.connect(
            lambda _k: self._mode_changed())
        effect_layout.addWidget(self.sg_mode2)
        sec.add_widget(self.w_effect_modes)
        self.mode_hint = CaptionLabel()
        self.mode_hint.setWordWrap(True)
        sec.add_widget(self.mode_hint)

        # 剪辑：起止时间
        self.w_clip = QWidget()
        self.clip_grid = FormGrid(columns=2)
        self.ed_start = LineEdit()
        self.ed_start.setPlaceholderText(tr("如 00:30 或 30", "e.g. 00:30 or 30"))
        self.ed_end = LineEdit()
        self.ed_end.setPlaceholderText(tr("留空表示到结尾", "blank = to end"))
        self.clip_grid.add_field(
            tr("开始时间", "Start time"), self.ed_start,
            hint=tr("留空表示从视频开头开始；支持秒数或 HH:MM:SS",
                    "Blank starts at the beginning; seconds or HH:MM:SS"))
        self.clip_grid.add_field(
            tr("结束时间", "End time"), self.ed_end,
            hint=tr("留空表示处理到视频结尾", "Blank continues to the end"))
        v1 = QVBoxLayout(self.w_clip)
        v1.setContentsMargins(0, 0, 0, 0)
        v1.setSpacing(8)
        v1.addLayout(self.clip_grid)
        # 专业剪辑器入口
        clip_btn_row = QHBoxLayout()
        clip_btn_row.setSpacing(8)
        self.btn_clip_editor = PushButton(
            FluentIcon.VIDEO, tr("打开剪辑器", "Open Clip Editor"))
        self.btn_clip_editor.setToolTip(
            tr("在独立窗口中进行视频剪辑（预览播放 + 时间轴入出点）",
               "Edit in a dedicated clip workspace (preview + timeline in/out)"))
        self.btn_clip_editor.clicked.connect(self._open_clip_editor)
        clip_btn_row.addWidget(self.btn_clip_editor)
        clip_btn_row.addStretch(1)
        v1.addLayout(clip_btn_row)
        # 高级调整：画面裁剪 / 锐化 / 降噪 / 色相 / 色温 / 去隔行
        self.advanced_section = CollapsibleSection(
            tr("高级调整", "Advanced adjustments"),
            tr("裁剪、锐化、降噪、色彩与去隔行，通常无需调整",
               "Crop, sharpen, denoise, color, and deinterlace"))
        from PySide6.QtWidgets import QDoubleSpinBox, QCheckBox, QSpinBox
        self.advanced_grid = FormGrid(columns=4)
        self.sb_crop_x = QSpinBox(); self.sb_crop_x.setRange(0, 10000)
        self.sb_crop_y = QSpinBox(); self.sb_crop_y.setRange(0, 10000)
        self.sb_crop_w = QSpinBox(); self.sb_crop_w.setRange(0, 10000); self.sb_crop_w.setValue(0)
        self.sb_crop_h = QSpinBox(); self.sb_crop_h.setRange(0, 10000); self.sb_crop_h.setValue(0)
        self.advanced_grid.add_field(tr("裁剪 X", "Crop X"), self.sb_crop_x)
        self.advanced_grid.add_field(tr("裁剪 Y", "Crop Y"), self.sb_crop_y)
        self.advanced_grid.add_field(tr("裁剪宽度（0 为关闭）", "Crop width (0 disables)"), self.sb_crop_w)
        self.advanced_grid.add_field(tr("裁剪高度（0 为关闭）", "Crop height (0 disables)"), self.sb_crop_h)
        self.sb_sharpen = QDoubleSpinBox(); self.sb_sharpen.setRange(0, 5); self.sb_sharpen.setSingleStep(0.2); self.sb_sharpen.setValue(0)
        self.sb_denoise = QDoubleSpinBox(); self.sb_denoise.setRange(0, 8); self.sb_denoise.setSingleStep(0.5); self.sb_denoise.setValue(0)
        self.sb_hue = QDoubleSpinBox(); self.sb_hue.setRange(-180, 180); self.sb_hue.setSingleStep(5); self.sb_hue.setValue(0)
        self.sb_wb = QDoubleSpinBox(); self.sb_wb.setRange(-0.5, 0.5); self.sb_wb.setSingleStep(0.05); self.sb_wb.setValue(0)
        self.advanced_grid.add_field(tr("锐化", "Sharpen"), self.sb_sharpen)
        self.advanced_grid.add_field(tr("降噪", "Denoise"), self.sb_denoise)
        self.advanced_grid.add_field(tr("色相（°）", "Hue (°)"), self.sb_hue)
        self.advanced_grid.add_field(tr("色温", "Color temperature"), self.sb_wb)
        self.cb_deinterlace = QCheckBox(tr("去隔行扫描(老视频)", "Deinterlace (old video)"))
        self.advanced_grid.add_field(tr("去隔行", "Deinterlace"), self.cb_deinterlace)
        self.advanced_section.add_layout(self.advanced_grid)
        v1.addWidget(self.advanced_section)
        sec.add_widget(self.w_clip)

        # 合并：预览排序入口
        self.w_merge = QWidget()
        mrow = QHBoxLayout(self.w_merge)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(8)
        self.btn_merge_preview = PushButton(
            FluentIcon.VIDEO, tr("打开合并预览器", "Open Merge Preview"))
        self.btn_merge_preview.setToolTip(
            tr("拖拽调整合并顺序，选中片段可预览播放",
               "Drag to reorder clips, click to preview"))
        self.btn_merge_preview.clicked.connect(self._open_merge_preview)
        mrow.addWidget(self.btn_merge_preview)
        mrow.addStretch(1)
        sec.add_widget(self.w_merge)

        # 字幕：字幕文件选择
        self.w_sub = QWidget()
        srow = QHBoxLayout(self.w_sub)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(8)
        self.ed_sub = LineEdit()
        self.ed_sub.setPlaceholderText(tr("选择 SRT / ASS 字幕文件…", "Pick SRT / ASS subtitle…"))
        self.ed_sub.setReadOnly(True)
        self.ed_sub.setAccessibleName(tr("字幕文件", "Subtitle file"))
        self.btn_sub = PushButton(FluentIcon.DOCUMENT, tr("选择字幕", "Pick"))
        self.btn_sub.clicked.connect(self._pick_subtitle)
        self.btn_edit_sub = PushButton(
            FluentIcon.EDIT, tr("编辑字幕", "Edit Subtitles"))
        self.btn_edit_sub.setToolTip(
            tr("在独立时间轴编辑器中调整字幕出现时间，配合视频预览对齐",
               "Adjust subtitle timing on a timeline with video preview"))
        self.btn_edit_sub.clicked.connect(self._open_subtitle_editor)
        srow.addWidget(self.ed_sub, 1)
        srow.addWidget(self.btn_sub)
        srow.addWidget(self.btn_edit_sub)
        sec.add_widget(self.w_sub)

        # 变速：倍速
        self.w_speed = QWidget()
        srow2 = QVBoxLayout(self.w_speed)
        srow2.setContentsMargins(0, 0, 0, 0)
        srow2.setSpacing(8)
        self.cb_speed = ComboBox()
        self.cb_speed.addItems(["0.5x", "0.75x", "1.25x", "1.5x",
                                "1.75x", "2.0x"])
        self.cb_speed.setCurrentText("1.5x")
        self.speed_grid = FormGrid(columns=2)
        self.speed_grid.add_field(tr("播放倍速", "Speed"), self.cb_speed)
        from PySide6.QtWidgets import QCheckBox as _QChk
        self.cb_interp = _QChk(
            tr("启用运动补偿补帧", "Enable motion interpolation"))
        self.speed_grid.add_field(tr("慢动作补帧", "Slow-motion interpolation"), self.cb_interp,
                                 hint=tr("仅在低于 1x 时可用，处理可能较慢", "Available below 1x; processing may be slower"))
        srow2.addLayout(self.speed_grid)
        sec.add_widget(self.w_speed)

        # 倒放：是否保留音轨
        self.w_reverse = QWidget()
        rrow = QHBoxLayout(self.w_reverse)
        rrow.setContentsMargins(0, 0, 0, 0)
        rrow.setSpacing(8)
        self.cb_rev_audio = _QChk(tr("同步倒放音轨", "Reverse audio too"))
        self.cb_rev_audio.setChecked(True)
        rrow.addWidget(self.cb_rev_audio)
        rrow.addStretch(1)
        sec.add_widget(self.w_reverse)

        # 转GIF：帧率 + 宽度
        self.w_gif = QWidget()
        grow = QVBoxLayout(self.w_gif)
        grow.setContentsMargins(0, 0, 0, 0)
        grow.setSpacing(8)
        self.cb_gif_fps = ComboBox()
        self.cb_gif_fps.addItems(["10", "12", "15", "20", "24"])
        self.cb_gif_fps.setCurrentText("15")
        self.cb_gif_w = ComboBox()
        self.cb_gif_w.addItems(["320", "480", "640", "800", "1024"])
        self.cb_gif_w.setCurrentText("480")
        self.gif_grid = FormGrid(columns=2)
        self.gif_grid.add_field(tr("帧率（fps）", "Frame rate (fps)"), self.cb_gif_fps)
        self.gif_grid.add_field(tr("输出宽度（px）", "Output width (px)"), self.cb_gif_w)
        grow.addLayout(self.gif_grid)
        sec.add_widget(self.w_gif)

        # 文字水印：文字/字号/位置/透明度
        self.w_watermark = QWidget()
        self.watermark_grid = gwm = FormGrid(columns=2)
        self.ed_wm_text = LineEdit()
        self.ed_wm_text.setPlaceholderText(tr("输入水印文字…", "Watermark text…"))
        self.cb_wm_pos = ComboBox()
        self.cb_wm_pos.addItems([tr("右下", "Bottom right"), tr("右上", "Top right"),
                                 tr("左下", "Bottom left"), tr("左上", "Top left"),
                                 tr("居中", "Center")])
        self.sb_wm_size = QSpinBox(); self.sb_wm_size.setRange(12, 200); self.sb_wm_size.setValue(48)
        self.sb_wm_alpha = QDoubleSpinBox(); self.sb_wm_alpha.setRange(0.1, 1.0)
        self.sb_wm_alpha.setSingleStep(0.1); self.sb_wm_alpha.setValue(0.7)
        gwm.add_field(tr("水印文字", "Text"), self.ed_wm_text)
        gwm.add_field(tr("位置", "Position"), self.cb_wm_pos)
        gwm.add_field(tr("字号", "Size"), self.sb_wm_size)
        gwm.add_field(tr("透明度", "Opacity"), self.sb_wm_alpha)
        vwm = QVBoxLayout(self.w_watermark)
        vwm.setContentsMargins(0, 0, 0, 0)
        vwm.addLayout(gwm)
        sec.add_widget(self.w_watermark)

        # 视频稳定：无参数，提示即可
        self.w_stabilize = QWidget()
        strow = QHBoxLayout(self.w_stabilize)
        strow.setContentsMargins(0, 0, 0, 0)
        strow.addWidget(CaptionLabel(
            tr("自动修复手持拍摄抖动（deshake）", "Auto-correct handheld shake (deshake)")))
        strow.addStretch(1)
        sec.add_widget(self.w_stabilize)

        # 音轨：选择音频 + 模式 + 背景音量
        self.w_track = QWidget()
        self.track_grid = gtk = FormGrid(columns=2)
        self.ed_track_audio = LineEdit()
        self.ed_track_audio.setReadOnly(True)
        self.ed_track_audio.setAccessibleName(tr("音频文件", "Audio file"))
        self.ed_track_audio.setPlaceholderText(tr("选择音频文件…", "Pick audio…"))
        self.btn_track_pick = PushButton(FluentIcon.MUSIC, tr("选择音频", "Pick"))
        self.btn_track_pick.clicked.connect(self._pick_track_audio)
        ha_w = QWidget()
        ha = QHBoxLayout(ha_w)
        ha.setContentsMargins(0, 0, 0, 0)
        ha.setSpacing(8)
        ha.addWidget(self.ed_track_audio, 1)
        ha.addWidget(self.btn_track_pick)
        gtk.add_field(tr("音频文件", "Audio"), ha_w)
        self.cb_track_mode = ComboBox()
        self.cb_track_mode.addItems([tr("替换原音轨", "Replace audio"),
                                     tr("混音(原声+背景)", "Mix with original")])
        self.sb_track_bg = QDoubleSpinBox(); self.sb_track_bg.setRange(0.0, 1.0)
        self.sb_track_bg.setSingleStep(0.1); self.sb_track_bg.setValue(0.3)
        gtk.add_field(tr("模式", "Mode"), self.cb_track_mode)
        gtk.add_field(tr("背景音量", "BG volume"), self.sb_track_bg)
        vtk = QVBoxLayout(self.w_track)
        vtk.setContentsMargins(0, 0, 0, 0)
        vtk.addLayout(gtk)
        sec.add_widget(self.w_track)

        # 去水印：水印区域（delogo 选区）
        self.w_delogo = QWidget()
        self.delogo_grid = FormGrid(columns=4)
        from PySide6.QtWidgets import QSpinBox
        self.sb_dx = QSpinBox(); self.sb_dx.setRange(0, 10000)
        self.sb_dy = QSpinBox(); self.sb_dy.setRange(0, 10000)
        self.sb_dw = QSpinBox(); self.sb_dw.setRange(1, 10000); self.sb_dw.setValue(120)
        self.sb_dh = QSpinBox(); self.sb_dh.setRange(1, 10000); self.sb_dh.setValue(60)
        self.delogo_grid.add_field(tr("区域 X", "Region X"), self.sb_dx)
        self.delogo_grid.add_field(tr("区域 Y", "Region Y"), self.sb_dy)
        self.delogo_grid.add_field(tr("区域宽度", "Region width"), self.sb_dw)
        self.delogo_grid.add_field(tr("区域高度", "Region height"), self.sb_dh)
        v2 = QVBoxLayout(self.w_delogo)
        v2.setContentsMargins(0, 0, 0, 0)
        v2.addLayout(self.delogo_grid)
        sec.add_widget(self.w_delogo)

        self._mode_changed()
        return sec

    def _pick_subtitle(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择字幕文件", "Pick subtitle file"), "",
            tr("字幕文件 (*.srt *.ass *.ssa *.vtt)", "Subtitle files (*.srt *.ass *.ssa *.vtt)"))
        if path:
            self.ed_sub.setText(path)

    def _open_subtitle_editor(self):
        """打开字幕时间轴编辑器（需要 SRT 文件 + 视频）。"""
        if not self.ed_sub.text().strip().lower().endswith(".srt"):
            from gui_qt.components import toast
            toast.show_warning(
                self, tr("请先选择 SRT 字幕文件（编辑器暂只支持 SRT）",
                         "Pick an SRT subtitle first (SRT only for now)"))
            return
        files = self.file_card.files()
        video = files[0] if files else ""
        from gui_qt.components.subtitle_editor import SubtitleTimelineDialog
        dlg = SubtitleTimelineDialog(
            self.ed_sub.text().strip(), video_path=video, parent=self)
        dlg.exec()
    def _pick_track_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择音频文件", "Pick audio file"), "",
            tr("音频文件 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg)",
               "Audio files (*.mp3 *.wav *.m4a *.aac *.flac *.ogg)"))
        if path:
            self.ed_track_audio.setText(path)

    def _mode_changed(self):
        mode = self.sg_mode.currentRouteKey()
        mode2 = self.sg_mode2.currentRouteKey()
        use_fx = self.sg_category.currentRouteKey() == "effects"
        self.w_basic_modes.setVisible(not use_fx)
        self.w_effect_modes.setVisible(use_fx)
        # 处理组
        self.w_clip.setVisible(not use_fx and mode == "clip")
        self.w_merge.setVisible(not use_fx and mode == "merge")
        self.w_sub.setVisible(not use_fx and mode == "subtitle")
        self.w_speed.setVisible(not use_fx and mode == "speed")
        self.w_delogo.setVisible(not use_fx and mode == "delogo")
        # 特效组
        self.w_reverse.setVisible(use_fx and mode2 == "reverse")
        self.w_gif.setVisible(use_fx and mode2 == "gif")
        self.w_watermark.setVisible(use_fx and mode2 == "watermark")
        self.w_stabilize.setVisible(use_fx and mode2 == "stabilize")
        self.w_track.setVisible(use_fx and mode2 == "track")
        self.mode_hint.setText(MODE_HINTS.get(mode2 if use_fx else mode, ""))
        self._sync_context_controls()
        # 提示文案随模式变化
        if use_fx:
            if mode2 == "reverse":
                self.file_card.set_target_fmt(tr("倒放处理", "Reverse"))
            elif mode2 == "gif":
                self.file_card.set_target_fmt(tr("转GIF", "To GIF"))
            elif mode2 == "watermark":
                self.file_card.set_target_fmt(tr("文字水印", "Text watermark"))
            elif mode2 == "stabilize":
                self.file_card.set_target_fmt(tr("视频稳定", "Stabilize"))
            elif mode2 == "track":
                self.file_card.set_target_fmt(tr("音轨处理", "Audio track"))
            return
        if mode == "merge":
            self.file_card.set_target_fmt(tr("合并为 1 个文件", "Merge into 1 file"))
        elif mode == "subtitle":
            self.file_card.set_target_fmt(tr("烧录字幕", "Burn subtitles"))
        elif mode == "speed":
            self.file_card.set_target_fmt(tr("变速处理", "Change Speed"))
        elif mode == "delogo":
            self.file_card.set_target_fmt(tr("去水印", "Remove logo"))
        else:
            self.file_card.set_target_fmt(tr("剪辑片段", "Clip"))

    # ── 专业剪辑器（独立窗口）───────────────────
    def _open_clip_editor(self):
        """打开独立剪辑窗口；确认后把入出点回填到起止时间框。"""
        files = self.file_card.files()
        if not files:
            from gui_qt.components import toast
            toast.show_warning(self, tr("请先添加要剪辑的视频",
                                        "Add a video to clip first"))
            return
        from gui_qt.components.clip_editor import ClipEditorDialog
        dlg = ClipEditorDialog(
            files[0],
            start=_parse_time(self.ed_start.text()) or 0.0,
            end=_parse_time(self.ed_end.text()),
            parent=self)
        if dlg.exec() == QDialog.Accepted:
            st, et = dlg.clip_range()
            if st is not None:
                self.ed_start.setText(f"{st:.2f}")
            if et is not None and et > 0:
                self.ed_end.setText(f"{et:.2f}")
            # 保存剪辑器里的变换/调整参数，导出时经 vf 滤镜应用
            self._clip_tool_params = dlg.tool_params()
            # 剪辑窗口「开始转换」：直接提交转换任务，无需再点一次开始
            self._submit_files()

    def _open_merge_preview(self):
        """打开合并预览器；确认后按新顺序提交合并任务。"""
        files = self.file_card.files()
        if len(files) < 2:
            from gui_qt.components import toast
            toast.show_warning(self, tr("合并至少需要 2 个视频文件",
                                        "Merge needs at least 2 videos"))
            return
        from gui_qt.components.merge_preview import MergePreviewDialog
        dlg = MergePreviewDialog(files, parent=self)
        if dlg.exec() == QDialog.Accepted:
            ordered = dlg.ordered_files()
            self.file_card.reorder(ordered)
            # 合并预览器「开始合并」：直接提交合并任务
            self._submit_files()

    # ── 视频信息（导入后显示时长/分辨率）─────────
    def _refresh_info(self):
        """文件变化 → 后台读取第一个视频的信息。"""
        files = self.file_card.files()
        if not files:
            self.lb_video_info.setText(
                tr("导入视频后显示时长与分辨率",
                   "Video duration & resolution will show here"))
            return
        fp = files[0]
        self.lb_video_info.setText(tr("正在读取视频信息…", "Reading video info…"))
        worker = _InfoWorker(fp, self)
        worker.sig_done.connect(self._on_info_done)
        worker.finished.connect(
            lambda: self._info_workers.remove(worker)
            if worker in self._info_workers else None)
        self._info_workers.append(worker)
        worker.start()

    def _on_info_done(self, fp, info):
        files = self.file_card.files()
        if not files or files[0] != fp:
            return  # 文件已变化，丢弃过期结果
        if not info:
            self.lb_video_info.setText(
                tr("无法读取视频信息", "Cannot read video info"))
            return
        parts = []
        dur = _to_seconds(info.get("duration"))
        if dur:
            parts.append(tr("时长", "Duration") + f": {_fmt_duration(dur)}")
        res = info.get("resolution")
        if res:
            parts.append(str(res))
        rate = info.get("frame_rate")
        if rate:
            try:
                fr = float(rate)
                if fr > 0:
                    parts.append(f"{fr:.0f}fps")
            except (TypeError, ValueError):
                pass
        self.lb_video_info.setText(" · ".join(parts))

    def collect_params(self) -> dict:
        mode = self.sg_mode.currentRouteKey()
        use_fx = self.sg_category.currentRouteKey() == "effects"
        mode2 = self.sg_mode2.currentRouteKey() if use_fx else "none"
        params = {
            "mode": mode,
            "mode2": mode2,
            "start": _parse_time(self.ed_start.text()),
            "end": _parse_time(self.ed_end.text()),
            "subtitle_path": self.ed_sub.text().strip(),
            "rate": float(self.cb_speed.currentText().replace("x", "")),
            "dl_x": self.sb_dx.value(), "dl_y": self.sb_dy.value(),
            "dl_w": self.sb_dw.value(), "dl_h": self.sb_dh.value(),
            # 高级调整（剪辑模式）
            "crop_x": self.sb_crop_x.value(), "crop_y": self.sb_crop_y.value(),
            "crop_w": self.sb_crop_w.value(), "crop_h": self.sb_crop_h.value(),
            "sharpen": self.sb_sharpen.value(), "denoise": self.sb_denoise.value(),
            "hue": self.sb_hue.value(), "wb": self.sb_wb.value(),
            "deinterlace": self.cb_deinterlace.isChecked(),
            # 变速补帧
            # 保留勾选记忆，但加速模式不把禁用的补帧选项传给执行器。
            "interp": self.cb_interp.isEnabled() and self.cb_interp.isChecked(),
            # 倒放
            "rev_audio": self.cb_rev_audio.isChecked(),
            # 转GIF
            "gif_fps": int(self.cb_gif_fps.currentText()),
            "gif_w": int(self.cb_gif_w.currentText()),
            # 文字水印
            "wm_text": self.ed_wm_text.text().strip(),
            "wm_pos": self.cb_wm_pos.currentIndex(),
            "wm_size": self.sb_wm_size.value(),
            "wm_alpha": self.sb_wm_alpha.value(),
            # 音轨
            "track_audio": self.ed_track_audio.text().strip(),
            "track_mode": self.cb_track_mode.currentIndex(),
            "track_bg": self.sb_track_bg.value(),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }
        if mode == "clip" and getattr(self, "_clip_tool_params", None):
            params["tool_params"] = dict(self._clip_tool_params)
        return params

    def collect_prefs(self) -> dict:
        return {
            "category": self.sg_category.currentRouteKey(),
            "mode": self.sg_mode.currentRouteKey(),
            "mode2": (self.sg_mode2.currentRouteKey()
                      if self.sg_category.currentRouteKey() == "effects"
                      else "none"),
            "out_dir_combo": self.out_row.mode(),
            "out_dir_path": self.out_row.path(),
        }

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        mode = prefs.get("mode")
        if mode in dict(MODES):
            self.sg_mode.setCurrentItem(mode)
            self._mode_changed()
        mode2 = prefs.get("mode2")
        if mode2 in dict(MODES2):
            self.sg_mode2.setCurrentItem(mode2)
        category = prefs.get("category")
        if category not in {"basic", "effects"}:
            # 兼容旧配置：旧版用 mode2=none 表示基础处理。
            category = "effects" if mode2 in dict(MODES2) else "basic"
        self.sg_category.setCurrentItem(category)
        self._mode_changed()
        if prefs.get("out_dir_combo") == OutputDirRow.MODE_CUSTOM:
            self.out_row.set_state(OutputDirRow.MODE_CUSTOM,
                                   prefs.get("out_dir_path", ""))

    # ── 任务 ─────────────────────────────────────
    def _start(self):
        if not self._validate_submission():
            return False
        return self._submit_files()

    def _validate_submission(self):
        """提交整批任务前只校验一次，避免每个文件重复弹出同一条提示。"""
        from gui_qt.components import toast

        use_fx = self.sg_category.currentRouteKey() == "effects"
        if use_fx:
            effect = self.sg_mode2.currentRouteKey()
            if effect == "watermark" and not self.ed_wm_text.text().strip():
                toast.show_warning(
                    self, tr("请先输入水印文字", "Enter watermark text"))
                self.ed_wm_text.setFocus()
                return False
            if effect == "track" and not self.ed_track_audio.text().strip():
                toast.show_warning(
                    self, tr("请先选择音频文件", "Pick an audio file"))
                self.btn_track_pick.setFocus()
                return False
            return True

        mode = self.sg_mode.currentRouteKey()
        if mode == "merge" and len(self.file_card.files()) < 2:
            toast.show_warning(
                self, tr("合并至少需要 2 个视频文件",
                         "Merge needs at least 2 video files"))
            return False
        if mode == "subtitle" and not self.ed_sub.text().strip():
            toast.show_warning(
                self, tr("请先选择字幕文件", "Pick a subtitle file first"))
            self.btn_sub.setFocus()
            return False
        if mode != "clip":
            return True

        start_text = self.ed_start.text().strip()
        end_text = self.ed_end.text().strip()
        start = _parse_time(start_text)
        end = _parse_time(end_text)
        if start_text and start is None:
            toast.show_warning(
                self, tr("开始时间格式无效，请输入秒数或 HH:MM:SS",
                         "Invalid start time; use seconds or HH:MM:SS"))
            self.ed_start.setFocus()
            return False
        if end_text and end is None:
            toast.show_warning(
                self, tr("结束时间格式无效，请输入秒数或 HH:MM:SS",
                         "Invalid end time; use seconds or HH:MM:SS"))
            self.ed_end.setFocus()
            return False
        if end is not None and end <= (start or 0.0):
            toast.show_warning(
                self, tr("结束时间必须晚于开始时间",
                         "End time must be later than start time"))
            self.ed_end.setFocus()
            return False
        return True

    def _empty_hint(self) -> str:
        return tr("请先添加要处理的视频文件", "Add videos to process first")

    def _sync_context_controls(self, *_args) -> None:
        """只更新适用性，不清空用户的参数，方便在各处理模式之间切换。"""
        self.cb_interp.setEnabled(float(self.cb_speed.currentText().rstrip("x")) < 1)
        self.sb_track_bg.setEnabled(self.cb_track_mode.currentIndex() == 1)
        files = self.file_card.files()
        self.btn_clip_editor.setEnabled(bool(files))
        self.btn_merge_preview.setEnabled(len(files) >= 2)
        subtitle = self.ed_sub.text().strip()
        self.btn_edit_sub.setEnabled(bool(files) and subtitle.lower().endswith(".srt")
                                     and os.path.isfile(subtitle))
        self.btn_edit_sub.setToolTip(tr("需要已添加的视频和本地 SRT 字幕文件", "Requires an added video and a local SRT subtitle file"))
        if hasattr(self, "action_bar") and hasattr(self, "_task_rows"):
            self._sync_start_enabled()

    def _sync_start_enabled(self):
        TaskPanelMixin._sync_start_enabled(self)
        if (self.sg_category.currentRouteKey() == "basic"
                and self.sg_mode.currentRouteKey() == "merge"
                and len(self.file_card.files()) < 2):
            self.action_bar.btn_go.setEnabled(False)
            self.action_bar.btn_go.setToolTip(tr("合并至少需要 2 个视频文件", "Merge needs at least 2 videos"))

    def _make_task(self, f: str) -> dict:
        mode = self.sg_mode.currentRouteKey()
        params = self.collect_params()
        mode2 = params["mode2"]
        files = self.file_card.files()
        fx = mode2 if mode2 != "none" else mode

        # ── 特效组（mode2 非 none 时优先）──
        if mode2 != "none":
            ext = os.path.splitext(f)[1] or ".mp4"
            if mode2 == "gif":
                out = tm.make_output_path(f, self.out_row.path(), ".gif")
                return dict(name=tr("视频转GIF", "Video to GIF"), task_type="video_tools",
                            file_path=f, output_path=out, params=params,
                            runner=self._runner, history_type=tr("视频处理", "Video Tools"),
                            history_target=tr("转GIF", "To GIF"), need_ffmpeg=True)
            if mode2 == "watermark":
                if not params["wm_text"]:
                    from gui_qt.components import toast
                    toast.show_warning(self, tr("请先输入水印文字", "Enter watermark text"))
                    return None
                out = tm.make_output_path(f, self.out_row.path(), ext)
                return dict(name=tr("文字水印", "Text watermark"), task_type="video_tools",
                            file_path=f, output_path=out, params=params,
                            runner=self._runner, history_type=tr("视频处理", "Video Tools"),
                            history_target=tr("文字水印", "Watermark"), need_ffmpeg=True)
            if mode2 == "track":
                if not params["track_audio"]:
                    from gui_qt.components import toast
                    toast.show_warning(self, tr("请先选择音频文件", "Pick an audio file"))
                    return None
                out = tm.make_output_path(f, self.out_row.path(), ext)
                target = tr("音轨替换", "Replace audio") if params["track_mode"] == 0 else tr("混音", "Mix")
                return dict(name=target, task_type="video_tools",
                            file_path=f, output_path=out, params=params,
                            runner=self._runner, history_type=tr("视频处理", "Video Tools"),
                            history_target=target, need_ffmpeg=True)
            if mode2 == "reverse":
                out = tm.make_output_path(f, self.out_row.path(), ext)
                return dict(name=tr("视频倒放", "Reverse"), task_type="video_tools",
                            file_path=f, output_path=out, params=params,
                            runner=self._runner, history_type=tr("视频处理", "Video Tools"),
                            history_target=tr("倒放", "Reverse"), need_ffmpeg=True)
            # stabilize
            out = tm.make_output_path(f, self.out_row.path(), ext)
            return dict(name=tr("视频稳定", "Stabilize"), task_type="video_tools",
                        file_path=f, output_path=out, params=params,
                        runner=self._runner, history_type=tr("视频处理", "Video Tools"),
                        history_target=tr("稳定", "Stabilize"), need_ffmpeg=True)

        if fx == "clip":
            start_text = self.ed_start.text().strip()
            end_text = self.ed_end.text().strip()
            if start_text and params["start"] is None:
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("开始时间格式无效，请输入秒数或 HH:MM:SS",
                             "Invalid start time; use seconds or HH:MM:SS"))
                self.ed_start.setFocus()
                return None
            if end_text and params["end"] is None:
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("结束时间格式无效，请输入秒数或 HH:MM:SS",
                             "Invalid end time; use seconds or HH:MM:SS"))
                self.ed_end.setFocus()
                return None
            start = params["start"] or 0.0
            if params["end"] is not None and params["end"] <= start:
                from gui_qt.components import toast
                toast.show_warning(
                    self, tr("结束时间必须晚于开始时间",
                             "End time must be later than start time"))
                self.ed_end.setFocus()
                return None
            ext = os.path.splitext(f)[1] or ".mp4"
            out = tm.make_output_path(f, self.out_row.path(), ext)
            return dict(name=tr("视频剪辑", "Video Clip"), task_type="video_tools",
                        file_path=f, output_path=out, params=params,
                        runner=self._runner,
                        history_type=tr("视频处理", "Video Tools"), history_target=tr("剪辑", "Clip"),
                        need_ffmpeg=True)
        if fx == "merge":
            if len(files) < 2:
                from gui_qt.components import toast
                toast.show_warning(self, tr("合并至少需要 2 个视频文件", "Merge needs at least 2 video files"))
                return None
            if f != files[0]:
                return None  # 合并只提交一个任务（携带全部文件）
            ext = os.path.splitext(files[0])[1] or ".mp4"
            out = tm.make_output_path(files[0], self.out_row.path(), ext)
            params = dict(params, files=files)
            return dict(name=tr("视频合并", "Video Merge"), task_type="video_tools",
                        file_path=f, output_path=out, params=params,
                        runner=self._runner,
                        history_type=tr("视频处理", "Video Tools"), history_target=tr("合并", "Merge"),
                        need_ffmpeg=True)
        if fx == "subtitle":
            if not params["subtitle_path"]:
                from gui_qt.components import toast
                toast.show_warning(self, tr("请先选择字幕文件", "Pick a subtitle file first"))
                return None
            out = tm.make_output_path(f, self.out_row.path(), ".mp4")
            return dict(name=tr("字幕烧录", "Burn Subtitle"), task_type="video_tools",
                        file_path=f, output_path=out, params=params,
                        runner=self._runner,
                        history_type=tr("视频处理", "Video Tools"), history_target=tr("字幕", "Subtitles"),
                        need_ffmpeg=True)
        # speed / delogo
        out = tm.make_output_path(f, self.out_row.path(),
                                  os.path.splitext(f)[1] or ".mp4")
        if fx == "delogo":
            return dict(name=tr("视频去水印", "Remove Logo"), task_type="video_tools",
                        file_path=f, output_path=out, params=params,
                        runner=self._runner, runner_key="video_tools",
                        history_type=tr("视频处理", "Video Tools"), history_target=tr("去水印", "Remove logo"),
                        need_ffmpeg=True)
        return dict(name=tr("视频变速", "Change Speed"), task_type="video_tools",
                    file_path=f, output_path=out, params=params,
                    runner=self._runner,
                    history_type=tr("视频处理", "Video Tools"), history_target=tr("变速", "Speed"),
                    need_ffmpeg=True)

    def _runner(self, task, prog):
        from core import video_tools
        p = task.params
        mode = p.get("mode", "clip")
        mode2 = p.get("mode2", "none")
        fx = mode2 if mode2 != "none" else mode
        # 特效组
        if mode2 == "reverse":
            return video_tools.reverse_video(task.file_path, task.output_path,
                                             keep_audio=bool(p.get("rev_audio", True)),
                                             progress_cb=prog)
        if mode2 == "gif":
            return video_tools.video_to_gif(task.file_path, task.output_path,
                                            fps=p.get("gif_fps", 15),
                                            max_width=p.get("gif_w", 480),
                                            progress_cb=prog)
        if mode2 == "watermark":
            positions = ("bottom_right", "top_right", "bottom_left",
                         "top_left", "center")
            try:
                position = positions[int(p.get("wm_pos", 0))]
            except (IndexError, TypeError, ValueError):
                position = positions[0]
            return video_tools.burn_text_watermark(
                task.file_path, task.output_path, p.get("wm_text", ""),
                font_size=p.get("wm_size", 48),
                position=position,
                opacity=p.get("wm_alpha", 0.7), progress_cb=prog)
        if mode2 == "stabilize":
            return video_tools.stabilize_video(task.file_path, task.output_path,
                                               progress_cb=prog)
        if mode2 == "track":
            if p.get("track_mode", 0) == 0:
                return video_tools.replace_audio(task.file_path,
                                                 p.get("track_audio", ""),
                                                 task.output_path, progress_cb=prog)
            return video_tools.mix_audio(task.file_path, p.get("track_audio", ""),
                                         task.output_path,
                                         bg_volume=p.get("track_bg", 0.3),
                                         progress_cb=prog)
        # 处理组
        if fx == "clip":
            return video_tools.clip_video(task.file_path, task.output_path,
                                          p.get("start"), p.get("end"),
                                          progress_cb=prog,
                                          vf=self._build_clip_vf(p))
        if fx == "merge":
            return video_tools.merge_videos(p.get("files") or [task.file_path],
                                            task.output_path, progress_cb=prog)
        if fx == "subtitle":
            return video_tools.burn_subtitle(task.file_path,
                                             p.get("subtitle_path", ""),
                                             task.output_path, progress_cb=prog)
        if fx == "delogo":
            return video_tools.remove_logo(
                task.file_path, task.output_path,
                p.get("dl_x", 0), p.get("dl_y", 0),
                p.get("dl_w", 120), p.get("dl_h", 60), progress_cb=prog)
        # speed：普通变速 / 补帧慢动作
        if p.get("interp") and 0 < float(p.get("rate", 1.0) or 1.0) < 1:
            return video_tools.slowmo_interp(task.file_path, task.output_path,
                                             p.get("rate", 0.5), target_fps=60,
                                             progress_cb=prog)
        return video_tools.change_speed(task.file_path, task.output_path,
                                        p.get("rate", 1.0), progress_cb=prog)

    @staticmethod
    def _build_clip_vf(p: dict):
        """把剪辑器里的变换/调整参数拼成 FFmpeg -vf 滤镜串；无参数返回 None。"""
        tp = (p.get("tool_params") or {})
        transform = tp.get("transform") or {}
        adjust = tp.get("adjust") or {}
        filters = []

        rot = int(transform.get("rotate", 0) or 0)
        if rot == 90:
            filters.append("transpose=1")
        elif rot == 180:
            filters.append("hflip,vflip")
        elif rot == 270:
            filters.append("transpose=2")
        if transform.get("hflip"):
            filters.append("hflip")
        if transform.get("vflip"):
            filters.append("vflip")

        eq = []
        if "brightness" in adjust and abs(float(adjust["brightness"])) > 0.001:
            eq.append(f"brightness={float(adjust['brightness']):.2f}")
        if "contrast" in adjust and abs(float(adjust["contrast"]) - 1.0) > 0.001:
            eq.append(f"contrast={float(adjust['contrast']):.2f}")
        if "saturation" in adjust and abs(float(adjust["saturation"]) - 1.0) > 0.001:
            eq.append(f"saturation={float(adjust['saturation']):.2f}")
        if eq:
            filters.append("eq=" + ":".join(eq))

        # ── 面板级高级调整（剪辑模式新增）──
        # 色相
        hue = float(p.get("hue", 0) or 0)
        if abs(hue) > 0.01:
            filters.append(f"hue=h={hue:.2f}")
        # 色温（colorbalance，正值偏暖/负值偏冷）
        wb = float(p.get("wb", 0) or 0)
        if abs(wb) > 0.01:
            if wb > 0:
                filters.append(f"colorbalance=rs=0.15:gs=0.05:bs=-0.15")
            else:
                filters.append(f"colorbalance=rs=-0.15:gs=-0.05:bs=0.15")
        # 锐化 / 降噪
        sharpen = float(p.get("sharpen", 0) or 0)
        if sharpen > 0:
            filters.append(f"unsharp=5:5:{min(5.0, sharpen):.2f}:5:5:0.0")
        denoise = float(p.get("denoise", 0) or 0)
        if denoise > 0:
            s = min(8.0, denoise)
            filters.append(f"hqdn3d={s:.1f}:{s:.1f}:{s:.1f}:{s:.1f}")
        # 去隔行
        if p.get("deinterlace"):
            filters.append("yadif=mode=1")
        # 画面裁剪（宽高 >0 才裁剪）
        cw, ch = int(p.get("crop_w", 0) or 0), int(p.get("crop_h", 0) or 0)
        if cw > 0 and ch > 0:
            cx, cy = int(p.get("crop_x", 0) or 0), int(p.get("crop_y", 0) or 0)
            filters.append(f"crop={cw}:{ch}:{cx}:{cy}")

        return ",".join(filters) if filters else None

    def resizeEvent(self, event):
        """窄窗口降低表单列数，避免像素参数和时间输入被压缩截断。"""
        super().resizeEvent(event)
        compact = self.viewport().width() < 900
        self.clip_grid.set_columns(1 if compact else 2)
        self.advanced_grid.set_columns(2 if compact else 4)
        self.delogo_grid.set_columns(2 if compact else 4)
        for grid in (self.speed_grid, self.gif_grid, self.watermark_grid, self.track_grid):
            grid.set_columns(1 if compact else 2)
