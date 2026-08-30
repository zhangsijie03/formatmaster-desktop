"""monitor_panel — 文件夹监视 + 剪贴板监视自动转换面板。

- 文件夹监视：QTimer 轮询监视目录，发现新文件自动提交转换任务
- 剪贴板监视：监听系统剪贴板，复制图片或文件自动转换（与文件夹监视
  共用目标类型/格式选择）
"""
import hashlib
import os

from PySide6.QtCore import QTimer, Signal
from gui_qt.components.safe_worker import SafeWorker
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout,
                               QWidget)
from qfluentwidgets import (CaptionLabel, ComboBox, FluentIcon, LineEdit,
                            PrimaryPushButton, PushButton)

from gui_qt.components import toast
from gui_qt.i18n import tr
from gui_qt.components.form_widgets import FormGrid, FormSection
from gui_qt.components.page_header import PageHeader
from gui_qt.panels.base_panel import BaseQtPanel

# 剪贴板图片动作：仅保存 / OCR 识别 / 保存并 OCR
CLIP_ACTIONS = [
    (tr("仅保存图片", "Save image"), "save"),
    (tr("OCR 识别文字", "OCR text"), "ocr"),
    (tr("保存并 OCR", "Save + OCR"), "save_ocr"),
]


class _OcrWorker(SafeWorker):
    """后台 OCR：RapidOCR 推理耗时，不能阻塞 UI 线程。"""

    sig_done = Signal(str, bool)   # (输出 txt 路径, 是否成功)

    def __init__(self, img_path, out_path, parent=None):
        super().__init__(parent)
        self._img = img_path
        self._out = out_path

    def work(self):
        from core.ocr_tool import ocr_to_file
        ok = ocr_to_file(self._img, self._out)
        self.sig_done.emit(self._out, ok)


VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
DOC_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt",
            ".pptx", ".ppt", ".md", ".html", ".epub", ".rtf", ".odt", ".ofd"}

# 输入扩展名按类型分组（监视时只转换匹配新文件）
INPUT_EXTS = {"video": VIDEO_EXTS, "audio": AUDIO_EXTS,
              "image": IMAGE_EXTS, "doc": DOC_EXTS}

# 目标格式：类型 → [(显示名, 输出扩展名, 音频/视频编码器或 None), ...]
KIND_LABELS = {"video": tr("视频", "Video"),
               "audio": tr("音频", "Audio"),
               "image": tr("图片", "Image"),
               "doc": tr("文档", "Docs")}
FORMAT_OPTIONS = {
    "video": [
        ("MP4", "mp4", None),
        ("MKV", "mkv", None),
        ("AVI", "avi", None),
        ("MOV", "mov", None),
        ("WebM", "webm", "libvpx-vp9"),   # WebM 不支持 H.264，需显式 VP9
    ],
    "audio": [
        ("MP3", "mp3", "mp3"),
        ("WAV", "wav", "pcm_s16le"),
        ("AAC", "aac", "aac"),
        ("FLAC", "flac", "flac"),
        ("OGG", "ogg", "libvorbis"),
        ("M4A", "m4a", "aac"),
    ],
    "image": [
        ("PNG", "png", None),
        ("JPG", "jpg", None),
        ("WebP", "webp", None),
        ("BMP", "bmp", None),
    ],
    "doc": [
        ("PDF", "pdf", None),
        ("DOCX", "docx", None),
        ("TXT", "txt", None),
        ("MD", "md", None),
    ],
}
KIND_KEYS = tuple(FORMAT_OPTIONS)
CLIP_ACTION_KEYS = tuple(action[1] for action in CLIP_ACTIONS)


def _remove_file_silent(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class MonitorPanelPage(BaseQtPanel):
    """文件夹监视页。"""

    panel_key = "monitor"

    def __init__(self, window, services, parent=None):
        self._timer = None
        self._seen = set()
        self._pending_files = {}
        self._ignored_outputs = set()
        self._reserved_outputs = set()
        self._submitted = 0
        self._running = False
        self._clip_running = False
        self._seen_clip_files = {}      # 路径 -> (size, mtime_ns)
        self._seen_img_keys = []        # 剪贴板已处理图片 hash（最近 200）
        self._ocr_workers = set()
        self._ocr_temp_paths = {}
        self._clip_image_seq = 0
        super().__init__(window, services, parent)

    def build(self):
        lay = self.content_layout
        self.header = PageHeader(
            tr("文件夹监视", "Folder Watch"),
            tr("等待新文件写入稳定后自动提交转换，也可监视剪贴板",
               "Automatically convert stable new files from a folder or the clipboard"),
            FluentIcon.FOLDER_ADD)
        lay.addWidget(self.header)

        # 监视目录 + 目标格式
        sec = FormSection(tr("监视设置", "Watch Settings"), FluentIcon.FOLDER_ADD)
        self.folder_hint = CaptionLabel(tr(
            "启动时不会处理目录中的已有文件。新增文件连续两次扫描保持稳定后才会提交；输出保存在监视目录，重名时自动追加 _converted。",
            "Existing files are ignored when watch starts. A new file is queued after two stable scans; output stays in the watched folder and uses _converted when needed."))
        self.folder_hint.setWordWrap(True)
        self.folder_hint.setProperty("sec", True)
        sec.add_widget(self.folder_hint)
        self.folder_grid = FormGrid(columns=2)
        self.ed_dir = LineEdit()
        self.ed_dir.setPlaceholderText(tr("选择要监视的文件夹…", "Pick a folder to watch…"))
        self.ed_dir.setReadOnly(True)
        self.btn_browse = PushButton(FluentIcon.FOLDER, tr("浏览", "Browse"))
        self.btn_browse.clicked.connect(self._pick_dir)
        folder_holder = QWidget(self)
        folder_row = QHBoxLayout(folder_holder)
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        folder_row.addWidget(self.ed_dir, 1)
        folder_row.addWidget(self.btn_browse)
        self.folder_grid.add_field(
            tr("监视目录", "Watch folder"), folder_holder, colspan=2)
        self.ed_dir.setAccessibleName(tr("监视目录", "Watch folder"))

        self.cb_kind = ComboBox()
        self.cb_kind.addItems([KIND_LABELS[k] for k in FORMAT_OPTIONS])
        self.folder_grid.add_field(tr("文件类型", "File type"), self.cb_kind)
        self.cb_fmt = ComboBox()
        self.folder_grid.add_field(tr("输出格式", "Output format"), self.cb_fmt)
        sec.add_form(self.folder_grid)

        # 开始/停止 + 状态
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self.btn_toggle = PrimaryPushButton(FluentIcon.PLAY, tr("开始监视", "Start"))
        self.btn_toggle.clicked.connect(self._toggle)
        ctrl.addWidget(self.btn_toggle)
        self.status_label = CaptionLabel(
            tr("未启动，仅处理新增文件", "Idle. New files only"))
        self.status_label.setProperty("sec", True)
        self.status_label.setAccessibleName(
            tr("文件夹监视状态", "Folder watch status"))
        ctrl.addWidget(self.status_label)
        ctrl.addStretch(1)
        sec.add_layout(ctrl)
        lay.addWidget(sec)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._scan)
        self.cb_kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

        # ── 剪贴板监视（同页，共用目标类型/格式）──
        clip_sec = FormSection(
            tr("剪贴板监视", "Clipboard Watch"), FluentIcon.PASTE)
        self.clip_hint = CaptionLabel("")
        self.clip_hint.setWordWrap(True)
        self.clip_hint.setProperty("sec", True)
        clip_sec.add_widget(self.clip_hint)

        self.clip_grid = FormGrid(columns=2)
        self.ed_clip_dir = LineEdit()
        self.ed_clip_dir.setText(os.path.join(
            os.path.expanduser("~"), "Desktop"))
        self.ed_clip_dir.setPlaceholderText(
            tr("选择或输入剪贴板输出目录…", "Pick or type the output folder…"))
        self.btn_clip_browse = PushButton(
            FluentIcon.FOLDER, tr("浏览", "Browse"))
        self.btn_clip_browse.clicked.connect(self._pick_clip_dir)
        clip_dir_holder = QWidget(self)
        clip_dir_row = QHBoxLayout(clip_dir_holder)
        clip_dir_row.setContentsMargins(0, 0, 0, 0)
        clip_dir_row.setSpacing(8)
        clip_dir_row.addWidget(self.ed_clip_dir, 1)
        clip_dir_row.addWidget(self.btn_clip_browse)
        self.clip_grid.add_field(
            tr("输出目录", "Output folder"), clip_dir_holder, colspan=2)
        self.ed_clip_dir.setAccessibleName(
            tr("剪贴板输出目录", "Clipboard output folder"))

        # 剪贴板独立的「目标类型 + 目标格式」（不跟文件夹监视共用）
        self.cb_clip_kind = ComboBox()
        self.cb_clip_kind.addItems([KIND_LABELS[k] for k in FORMAT_OPTIONS])
        self.clip_grid.add_field(
            tr("文件类型", "File type"), self.cb_clip_kind)
        self.cb_clip_fmt = ComboBox()
        self.clip_grid.add_field(
            tr("输出格式", "Output format"), self.cb_clip_fmt)
        self.cb_clip_kind.currentIndexChanged.connect(self._clip_kind_changed)

        self.cb_clip_act = ComboBox()
        self.cb_clip_act.addItems([a[0] for a in CLIP_ACTIONS])
        self.cb_clip_act.setToolTip(
            tr("复制截图后自动执行的动作", "Action after copying an image"))
        self.clip_grid.add_field(
            tr("截图动作", "Image action"), self.cb_clip_act,
            hint=tr("仅在文件类型为图片时生效",
                    "Available only when File type is Image"),
            colspan=2)
        self._clip_kind_changed()
        clip_sec.add_form(self.clip_grid)

        clip_ctrl = QHBoxLayout()
        clip_ctrl.setSpacing(8)
        self.btn_clip_toggle = PrimaryPushButton(
            FluentIcon.PLAY, tr("开始监视", "Start watch"))
        self.btn_clip_toggle.clicked.connect(self._toggle_clip)
        clip_ctrl.addWidget(self.btn_clip_toggle)
        self.clip_status = CaptionLabel(tr("未监视剪贴板", "Clipboard idle"))
        self.clip_status.setProperty("sec", True)
        self.clip_status.setAccessibleName(
            tr("剪贴板监视状态", "Clipboard watch status"))
        clip_ctrl.addWidget(self.clip_status)
        clip_ctrl.addStretch(1)
        clip_sec.add_layout(clip_ctrl)
        lay.addWidget(clip_sec)

    def _kind_changed(self):
        """大类切换 → 更新目标格式下拉选项。

        监视运行中切换目标类型时，重建 _seen 快照，避免该类型下已存在的
        文件被当成「新文件」批量误转（_seen 是旧类型扩展名的集合）。
        """
        kind = list(FORMAT_OPTIONS)[self.cb_kind.currentIndex()]
        self.cb_fmt.clear()
        self.cb_fmt.addItems([opt[0] for opt in FORMAT_OPTIONS[kind]])
        if self._running and getattr(self, "_dir", None):
            exts = INPUT_EXTS[kind]
            try:
                self._seen = {f for f in self._list_files(self._dir)
                              if f.lower().endswith(tuple(exts))}
            except OSError:
                pass

    def _clip_kind_changed(self):
        """剪贴板监视独立的目标类型切换 → 联动格式下拉。"""
        kind = list(FORMAT_OPTIONS)[self.cb_clip_kind.currentIndex()]
        self.cb_clip_fmt.clear()
        self.cb_clip_fmt.addItems([opt[0] for opt in FORMAT_OPTIONS[kind]])
        if hasattr(self, "cb_clip_act"):
            is_image = kind == "image"
            self.cb_clip_act.setEnabled(is_image and not self._clip_running)
            self.clip_hint.setText(tr(
                "复制图片数据或图片文件后，按截图动作处理并保存到输出目录。",
                "Copied image data or image files are processed using Image action and saved to the output folder.") if is_image else tr(
                    "只处理剪贴板中与所选文件类型匹配的本地文件；复制的图片数据会被忽略。",
                    "Only local files matching the selected File type are processed; copied image data is ignored."))

    def _clip_target(self):
        """剪贴板监视独立的目标 (kind, ext, codec)。"""
        kind = list(FORMAT_OPTIONS)[self.cb_clip_kind.currentIndex()]
        _label, ext, codec = FORMAT_OPTIONS[kind][self.cb_clip_fmt.currentIndex()]
        return kind, ext, codec

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, tr("选择监视目录", "Pick watch folder"))
        if d:
            self.ed_dir.setText(d)

    def _toggle(self):
        d = self.ed_dir.text().strip()
        if not d or not os.path.isdir(d):
            toast.show_warning(self, tr("请先选择有效的监视目录", "Pick a valid folder first"))
            return
        if self._running:
            self._stop()
        else:
            self._start(d)

    def _start(self, d):
        # 初始快照：已存在的文件不转换
        kind, _ext, _codec = self._target()
        exts = INPUT_EXTS[kind]
        try:
            self._seen = {
                f for f in self._list_files(d)
                if f.lower().endswith(tuple(exts))}
        except OSError as exc:
            toast.show_error(
                self, tr("无法读取监视目录：{}",
                         "Cannot read watch folder: {}").format(exc))
            return
        self._running = True
        self._dir = d
        self._pending_files.clear()
        self._submitted = 0
        self._set_folder_controls_enabled(False)
        self._timer.start()
        self.btn_toggle.setText(tr("停止监视", "Stop watch"))
        self.btn_toggle.setIcon(FluentIcon.CANCEL)
        self.status_label.setText(
            tr("监视中：{}，输出 {}",
               "Watching {}. Output: {}").format(
                   os.path.basename(d), self.cb_fmt.currentText()))
        toast.show_success(self, tr("开始监视文件夹", "Now watching folder"))

    def _stop(self):
        self._running = False
        self._timer.stop()
        self._pending_files.clear()
        self._set_folder_controls_enabled(True)
        self.btn_toggle.setText(tr("开始监视", "Start watch"))
        self.btn_toggle.setIcon(FluentIcon.PLAY)
        self.status_label.setText(
            tr("已停止（本次提交 {} 个任务）",
               "Stopped ({} tasks queued)").format(self._submitted))

    def _set_folder_controls_enabled(self, enabled):
        """监视期间锁定目录和转换目标，避免快照与实际规则错位。"""
        for control in (self.btn_browse, self.cb_kind, self.cb_fmt):
            control.setEnabled(enabled)

    def _target(self):
        kind = list(FORMAT_OPTIONS)[self.cb_kind.currentIndex()]
        _label, ext, codec = FORMAT_OPTIONS[kind][self.cb_fmt.currentIndex()]
        return kind, ext, codec

    def _list_files(self, d):
        return sorted(
            os.path.join(d, name) for name in os.listdir(d)
            if os.path.isfile(os.path.join(d, name)))

    @staticmethod
    def _file_signature(path):
        stat = os.stat(path)
        return stat.st_size, stat.st_mtime_ns

    def _scan(self):
        if not self._running:
            return
        d = self._dir
        kind, _ext, _codec = self._target()
        exts = INPUT_EXTS[kind]
        configured_dir = self.ed_dir.text().strip()
        if configured_dir and configured_dir != d and os.path.isdir(configured_dir):
            # 正常 UI 在监视期间会锁定目录；若偏好或外部代码
            # 仍更改了路径，切换时先建快照，不误转新目录已有文件。
            try:
                existing = {
                    path for path in self._list_files(configured_dir)
                    if path.lower().endswith(tuple(exts))}
            except OSError:
                existing = None
            if existing is not None:
                self._dir = configured_dir
                self._seen = existing
                self._pending_files.clear()
                self.status_label.setText(
                    tr("监视中：{}", "Watching: {}").format(
                        os.path.basename(configured_dir)))
                return
        try:
            now = {f for f in self._list_files(d)
                   if f.lower().endswith(tuple(exts))}
        except OSError as exc:
            self._stop()
            self.status_label.setText(
                tr("监视已停止：目录无法读取",
                   "Watch stopped: folder is unavailable"))
            toast.show_error(
                self, tr("监视目录无法读取：{}",
                         "Watch folder is unavailable: {}").format(exc))
            return

        self._seen.intersection_update(now)
        candidates = {
            path for path in now - self._seen
            if self._path_key(path) not in self._ignored_outputs}
        for f in sorted(candidates):
            try:
                signature = self._file_signature(f)
            except OSError:
                continue
            # 连续两次扫描大小与修改时间均不变才提交，
            # 避免在大文件仍处于复制/下载时读取半截内容。
            if self._pending_files.get(f) != signature:
                self._pending_files[f] = signature
                continue
            try:
                self._convert(f)
            except Exception as exc:  # noqa: BLE001 - 单文件失败需反馈并继续监视
                self.status_label.setText(
                    tr("提交失败：{}", "Queue failed: {}").format(
                        os.path.basename(f)))
                toast.show_error(
                    self, tr("无法提交 {}：{}",
                             "Could not queue {}: {}").format(
                                 os.path.basename(f), exc))
            self._seen.add(f)
            self._pending_files.pop(f, None)
        for path in set(self._pending_files) - candidates:
            self._pending_files.pop(path, None)

    def _convert(self, path):
        kind, ext, codec = self._target()
        self._submit_conversion(kind, ext, codec, path, self._dir)

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path)).casefold()

    def _unique_output_path(self, out_dir, path, ext):
        """输出绝不覆盖源文件、已有文件或本会话已预留目标。"""
        stem = os.path.splitext(os.path.basename(path))[0]
        source_key = self._path_key(path)
        index = 0
        while True:
            suffix = "" if index == 0 else (
                "_converted" if index == 1 else f"_converted_{index}")
            candidate = os.path.join(out_dir, f"{stem}{suffix}.{ext}")
            key = self._path_key(candidate)
            if (key != source_key and key not in self._reserved_outputs
                    and not os.path.exists(candidate)):
                return candidate
            index += 1

    def _submit_conversion(self, kind, ext, codec, path, out_dir):
        """把单个文件提交为转换任务（文件夹/剪贴板监视共用）。"""
        mgr = self.services.task_manager
        if not os.path.isfile(path):
            raise FileNotFoundError(tr("源文件已不存在",
                                       "Source file no longer exists"))
        if not os.path.isdir(out_dir):
            raise FileNotFoundError(tr("输出目录不存在",
                                       "Output folder does not exist"))
        out = self._unique_output_path(out_dir, path, ext)
        out_key = self._path_key(out)
        self._reserved_outputs.add(out_key)
        self._ignored_outputs.add(out_key)
        label = ext.upper()
        if kind == "video":
            # WebM 等需要特定编码器（选项里已配置，如 libvpx-vp9）
            vcodec = codec

            def runner(task, prog):
                return self.services.video_conv.convert(
                    task.file_path, task.output_path, ext, codec=vcodec,
                    progress_callback=prog)
            need_ffmpeg = True
        elif kind == "audio":

            def runner(task, prog):
                return self.services.audio_conv.convert(
                    task.file_path, task.output_path, codec,
                    progress_callback=prog)
            need_ffmpeg = True
        elif kind == "doc":

            def runner(task, prog):
                return self.services.doc_conv.convert(
                    task.file_path, task.output_path, progress_callback=prog)
            need_ffmpeg = False
        else:

            def runner(task, prog):
                return self.services.image_conv.convert(
                    task.file_path, task.output_path, progress_callback=prog)
            need_ffmpeg = False
        try:
            task_id = mgr.add_task(
                name=tr("监视转换", "Watch & convert"),
                task_type="monitor", file_path=path, output_path=out,
                params={}, runner=runner, need_ffmpeg=need_ffmpeg,
                history_type=tr("文件夹监视", "Folder Watch"),
                history_target=label)
        except Exception:
            self._reserved_outputs.discard(out_key)
            self._ignored_outputs.discard(out_key)
            raise
        if task_id is None:
            self._reserved_outputs.discard(out_key)
            self._ignored_outputs.discard(out_key)
            raise RuntimeError(tr("转换引擎未就绪",
                                  "Conversion engine is not ready"))
        self._submitted += 1
        return task_id

    # ── 剪贴板监视 ──────────────────────────────
    def _pick_clip_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("选择剪贴板输出目录", "Pick clipboard output folder"))
        if d:
            self.ed_clip_dir.setText(d)

    def _toggle_clip(self):
        if self._clip_running:
            self._stop_clip()
        else:
            self._start_clip()

    def _start_clip(self):
        d = self.ed_clip_dir.text().strip()
        if not d or not os.path.isdir(d):
            toast.show_warning(self, tr("请先选择有效的剪贴板输出目录",
                                        "Pick a valid output folder first"))
            return
        self._clip_running = True
        self._seen_clip_files.clear()
        self._clip = QApplication.clipboard()
        self._clip.dataChanged.connect(self._on_clip_changed)
        self._set_clip_controls_enabled(False)
        self.btn_clip_toggle.setText(tr("停止监视", "Stop watch"))
        self.btn_clip_toggle.setIcon(FluentIcon.CANCEL)
        self.clip_status.setText(
            tr("监视中：{} 文件输出为 {}",
               "Watching clipboard for {} files, output as {}").format(
                   self.cb_clip_kind.currentText(),
                   self.cb_clip_fmt.currentText()))
        toast.show_success(self, tr("开始剪贴板监视", "Now watching clipboard"))

    def _stop_clip(self):
        self._clip_running = False
        try:
            self._clip.dataChanged.disconnect(self._on_clip_changed)
        except (RuntimeError, TypeError):
            pass
        self._set_clip_controls_enabled(True)
        self.btn_clip_toggle.setText(tr("开始监视", "Start watch"))
        self.btn_clip_toggle.setIcon(FluentIcon.PLAY)
        self.clip_status.setText(tr("已停止剪贴板监视", "Clipboard idle"))

    def _set_clip_controls_enabled(self, enabled):
        """剪贴板监视期间固定输出与动作，保证事件上下文一致。"""
        for control in (self.ed_clip_dir, self.btn_clip_browse,
                        self.cb_clip_kind, self.cb_clip_fmt):
            control.setEnabled(enabled)
        is_image = KIND_KEYS[self.cb_clip_kind.currentIndex()] == "image"
        self.cb_clip_act.setEnabled(enabled and is_image)

    def _on_clip_changed(self):
        if not self._clip_running:
            return
        try:
            mime = self._clip.mimeData()
        except RuntimeError:
            return
        if mime is None:
            return
        out_dir = self.ed_clip_dir.text().strip()
        if not os.path.isdir(out_dir):
            self._stop_clip()
            self.clip_status.setText(
                tr("剪贴板监视已停止：输出目录无效",
                   "Clipboard watch stopped: output folder is invalid"))
            toast.show_error(
                self, tr("剪贴板输出目录已不可用，请重新选择",
                         "Clipboard output folder is unavailable; choose it again"))
            return
        kind, ext, codec = self._clip_target()
        # ① 剪贴板图片 → 按所选动作处理
        if kind == "image" and mime.hasImage():
            img = self._clip.image()
            if not img.isNull() and self._clip_img_new(img):
                self._save_clip_image(img, ext, out_dir)
            return
        # ② 剪贴板文件 → 匹配类型则转换
        if mime.hasUrls():
            for url in mime.urls():
                if not url.isLocalFile():
                    continue
                fp = url.toLocalFile()
                if not os.path.isfile(fp):
                    continue
                if not fp.lower().endswith(tuple(INPUT_EXTS[kind])):
                    continue
                try:
                    signature = self._file_signature(fp)
                except OSError:
                    continue
                if self._seen_clip_files.get(fp) == signature:
                    continue
                try:
                    self._submit_conversion(kind, ext, codec, fp, out_dir)
                except Exception as exc:  # noqa: BLE001 - 剪贴板回调不能向 Qt 事件环抛出
                    self.clip_status.setText(
                        tr("剪贴板文件提交失败",
                           "Clipboard file could not be queued"))
                    toast.show_error(
                        self, tr("无法提交 {}：{}",
                                 "Could not queue {}: {}").format(
                                     os.path.basename(fp), exc))
                    continue
                self._seen_clip_files[fp] = signature
                if len(self._seen_clip_files) > 500:
                    self._seen_clip_files.pop(next(iter(self._seen_clip_files)))
                self.clip_status.setText(
                    tr("已提交：{}，继续监视中",
                       "Queued {}. Watch continues").format(
                           os.path.basename(fp)))
                toast.show_success(
                    self, tr("剪贴板文件已加入转换", "Clipboard file converting")
                    + f"  {os.path.basename(fp)}")

    def _clip_img_new(self, img):
        """按完整像素与尺寸 hash 去重，避免缩略图碰撞丢图。"""
        normalized = img.convertToFormat(QImage.Format_RGBA8888)
        try:
            digest = hashlib.sha256()
            digest.update(
                f"{normalized.width()}x{normalized.height()}|"
                f"{normalized.bytesPerLine()}".encode("ascii"))
            digest.update(bytes(normalized.bits()))
            key = digest.hexdigest()
        except (BufferError, ValueError):
            return False
        if key in self._seen_img_keys:
            return False
        self._seen_img_keys.append(key)
        if len(self._seen_img_keys) > 200:
            self._seen_img_keys = self._seen_img_keys[-200:]
        return True

    def _save_clip_image(self, img, ext, out_dir):
        """按「截图动作」处理剪贴板图片：保存 / OCR / 保存并 OCR。"""
        name = tr("剪贴板", "clipboard")
        self._clip_image_seq += 1
        seq = self._clip_image_seq
        action = CLIP_ACTIONS[self.cb_clip_act.currentIndex()][1]
        need_save = action in ("save", "save_ocr")
        img_path = ""
        if need_save:
            path = os.path.join(out_dir, f"{name}_{seq}.{ext}")
            while os.path.exists(path):
                self._clip_image_seq += 1
                seq = self._clip_image_seq
                path = os.path.join(out_dir, f"{name}_{seq}.{ext}")
            fmt = "jpg" if ext == "jpg" else ext
            if img.save(path, fmt):
                img_path = path
                self.clip_status.setText(
                    tr("已保存：{}，继续监视中",
                       "Saved {}. Watch continues").format(
                           os.path.basename(path)))
                toast.show_success(
                    self, tr("剪贴板图片已转换", "Clipboard image converted")
                    + f"  {os.path.basename(path)}")
            else:
                self.clip_status.setText(
                    tr("剪贴板图片保存失败",
                       "Clipboard image save failed"))
                toast.show_error(
                    self, tr("无法保存剪贴板图片，请检查目录权限",
                             "Could not save clipboard image; check folder permissions"))
                return
        if action in ("ocr", "save_ocr"):
            cleanup_temp = False
            if not img_path:
                img_path = os.path.join(out_dir, f".{name}_{seq}_ocr.png")
                out_txt = os.path.join(out_dir, f"{name}_{seq}.txt")
                while os.path.exists(img_path) or os.path.exists(out_txt):
                    self._clip_image_seq += 1
                    seq = self._clip_image_seq
                    img_path = os.path.join(out_dir, f".{name}_{seq}_ocr.png")
                    out_txt = os.path.join(out_dir, f"{name}_{seq}.txt")
                if not img.save(img_path, "png"):
                    self.clip_status.setText(
                        tr("OCR 临时图片保存失败",
                           "Could not prepare image for OCR"))
                    toast.show_error(
                        self, tr("无法准备 OCR 图片，请检查目录权限",
                                 "Could not prepare the OCR image; check folder permissions"))
                    return
                cleanup_temp = True
            else:
                out_txt = os.path.splitext(img_path)[0] + ".txt"
            self.clip_status.setText(tr("正在识别截图文字…", "OCR in progress…"))
            worker = _OcrWorker(img_path, out_txt, self)
            self._ocr_workers.add(worker)
            if cleanup_temp:
                self._ocr_temp_paths[worker] = img_path
            worker.sig_done.connect(
                lambda output, ok, current=worker:
                self._on_ocr_done(current, output, ok))
            worker.sig_error.connect(
                lambda message, current=worker:
                self._on_ocr_error(current, message))
            worker.finished.connect(
                lambda current=worker: self._on_ocr_finished(current))
            worker.start()

    def _cleanup_ocr_temp(self, worker):
        temp_path = self._ocr_temp_paths.pop(worker, "")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _on_ocr_done(self, worker, out_txt, ok):
        self._cleanup_ocr_temp(worker)
        if not self._clip_running:
            return
        if ok and os.path.isfile(out_txt):
            self.clip_status.setText(
                tr("OCR 完成，继续监视中",
                   "OCR complete. Watch continues"))
            toast.show_success(
                self, tr("截图文字已识别", "Clipboard text recognized")
                + f"  {os.path.basename(out_txt)}")
        else:
            self.clip_status.setText(tr("OCR 未识别到文字", "OCR no text"))
            toast.show_warning(self, tr("OCR 未识别到文字", "No text recognized"))

    def _on_ocr_error(self, worker, message):
        self._cleanup_ocr_temp(worker)
        if not self._clip_running:
            return
        self.clip_status.setText(tr("OCR 失败", "OCR failed"))
        toast.show_error(
            self, tr("OCR 失败：{}", "OCR failed: {}").format(message))

    def _on_ocr_finished(self, worker):
        self._cleanup_ocr_temp(worker)
        self._ocr_workers.discard(worker)
        worker.deleteLater()

    def collect_prefs(self) -> dict:
        """记忆监视目录/目标类型/剪贴板设置，重进面板自动恢复。"""
        kind = KIND_KEYS[self.cb_kind.currentIndex()]
        clip_kind = KIND_KEYS[self.cb_clip_kind.currentIndex()]
        return {"dir": self.ed_dir.text().strip(),
                "kind": kind,
                "fmt": FORMAT_OPTIONS[kind][self.cb_fmt.currentIndex()][1],
                "clip_dir": self.ed_clip_dir.text().strip(),
                "clip_kind": clip_kind,
                "clip_fmt": FORMAT_OPTIONS[clip_kind][
                    self.cb_clip_fmt.currentIndex()][1],
                "clip_act": CLIP_ACTION_KEYS[self.cb_clip_act.currentIndex()]}

    def apply_prefs(self, prefs: dict):
        if not prefs:
            return
        if prefs.get("dir"):
            self.ed_dir.setText(prefs["dir"])
        if prefs.get("clip_dir"):
            self.ed_clip_dir.setText(prefs["clip_dir"])
        kind_value = prefs.get("kind")
        if isinstance(kind_value, str) and kind_value in KIND_KEYS:
            self.cb_kind.setCurrentIndex(KIND_KEYS.index(kind_value))
        elif isinstance(kind_value, int) and 0 <= kind_value < len(KIND_KEYS):
            self.cb_kind.setCurrentIndex(kind_value)
        kind = KIND_KEYS[self.cb_kind.currentIndex()]
        fmt_value = prefs.get("fmt")
        fmt_keys = [option[1] for option in FORMAT_OPTIONS[kind]]
        if isinstance(fmt_value, str) and fmt_value in fmt_keys:
            self.cb_fmt.setCurrentIndex(fmt_keys.index(fmt_value))
        elif isinstance(fmt_value, int) and 0 <= fmt_value < self.cb_fmt.count():
            self.cb_fmt.setCurrentIndex(fmt_value)

        clip_kind_value = prefs.get("clip_kind")
        if isinstance(clip_kind_value, str) and clip_kind_value in KIND_KEYS:
            self.cb_clip_kind.setCurrentIndex(KIND_KEYS.index(clip_kind_value))
        elif (isinstance(clip_kind_value, int)
              and 0 <= clip_kind_value < len(KIND_KEYS)):
            self.cb_clip_kind.setCurrentIndex(clip_kind_value)
        clip_kind = KIND_KEYS[self.cb_clip_kind.currentIndex()]
        clip_fmt_value = prefs.get("clip_fmt")
        clip_fmt_keys = [option[1] for option in FORMAT_OPTIONS[clip_kind]]
        if isinstance(clip_fmt_value, str) and clip_fmt_value in clip_fmt_keys:
            self.cb_clip_fmt.setCurrentIndex(clip_fmt_keys.index(clip_fmt_value))
        elif (isinstance(clip_fmt_value, int)
              and 0 <= clip_fmt_value < self.cb_clip_fmt.count()):
            self.cb_clip_fmt.setCurrentIndex(clip_fmt_value)

        action_value = prefs.get("clip_act")
        if isinstance(action_value, str) and action_value in CLIP_ACTION_KEYS:
            self.cb_clip_act.setCurrentIndex(CLIP_ACTION_KEYS.index(action_value))
        elif (isinstance(action_value, int)
              and 0 <= action_value < self.cb_clip_act.count()):
            self.cb_clip_act.setCurrentIndex(action_value)

    def resizeEvent(self, event):
        """窄窗口将两组配置都降为单列，保持路径与状态可读。"""
        super().resizeEvent(event)
        columns = 1 if self.width() < 760 else 2
        if hasattr(self, "folder_grid"):
            self.folder_grid.set_columns(columns)
            self.clip_grid.set_columns(columns)

    def closeEvent(self, event):
        """关闭页面时停止所有事件源，慢 OCR 线程脱离页面后自行回收。"""
        if self._running:
            self._stop()
        if self._clip_running:
            self._stop_clip()
        for worker in list(self._ocr_workers):
            worker.stop()
            if worker.wait(500):
                self._cleanup_ocr_temp(worker)
                worker.deleteLater()
            else:
                for signal in (worker.sig_done, worker.sig_error,
                               worker.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                temp_path = self._ocr_temp_paths.pop(worker, "")
                worker.setParent(None)
                worker.finished.connect(worker.deleteLater)
                if temp_path:
                    worker.finished.connect(
                        lambda path=temp_path: _remove_file_silent(path))
        self._ocr_workers.clear()
        super().closeEvent(event)
