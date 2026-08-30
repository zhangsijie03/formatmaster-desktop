"""video_merge — 批量视频合并（按列表顺序合并为单个 MP4）。

合并策略：
1. 先探测全部输入：视频编码、分辨率、是否含音频流一致时，
   使用 concat demuxer + `-c copy` 流复制（秒级完成，不损失画质）；
2. 编码/分辨率不一致，或流复制失败时，回退为 filter_complex
   重编码：统一缩放到第一个视频的分辨率（等比裁剪留黑边），
   音频统一 aformat 后 concat，libx264 + aac 输出。

统一遵循项目规范：get_ffmpeg_path()、
run_ffmpeg 进度解析；不依赖 GUI。
"""
import os
import tempfile

from core.ffmpeg_executor import get_ffprobe_raw
from core.ffmpeg_progress import run_ffmpeg
from utils.config import get_ffmpeg_path


def _duration_of(path):
    """用 ffprobe 获取媒体时长（秒），失败返回 0。"""
    try:
        info = get_ffprobe_raw(path, timeout=10)
        if info and "format" in info:
            return float(info["format"].get("duration", 0))
    except Exception:  # noqa: BLE001 - 探测失败按 0 处理
        pass
    return 0.0


def _video_streams(path):
    """返回 [(codec_name, width, height, has_audio), ...]，失败返回 []。"""
    try:
        info = get_ffprobe_raw(path, timeout=10)
        if not info:
            return []
        video = None
        has_audio = False
        for s in info.get("streams", []):
            ctype = s.get("codec_type", "")
            if ctype == "video" and video is None:
                video = (s.get("codec_name", ""), s.get("width", 0),
                         s.get("height", 0))
            elif ctype == "audio":
                has_audio = True
        if video is None:
            return []
        codec, width, height = video
        return [(codec, int(width or 0), int(height or 0), has_audio)]
    except Exception:  # noqa: BLE001
        return []


def _probe_compatible(paths):
    """判断所有输入编码/分辨率/音频结构是否一致。

    返回 (compatible, 首个有效分辨率, 每个输入是否含音频)。兼容性结果
    与回退参数分离，避免一处规格不同就把分辨率和音频信息一起丢掉。
    """
    first = None
    compatible = True
    resolution = None
    audio_flags = []
    for p in paths:
        streams = _video_streams(p)
        if not streams:
            compatible = False
            audio_flags.append(False)
            continue
        codec, width, height, has_audio = streams[0]
        audio_flags.append(has_audio)
        if width <= 0 or height <= 0:
            compatible = False
            continue
        if resolution is None:
            resolution = (width, height)
        if first is None:
            first = (codec, width, height, has_audio)
        elif (codec, width, height, has_audio) != first:
            compatible = False
    return compatible and first is not None, resolution, tuple(audio_flags)


def _write_concat_list(paths, list_path):
    """写入 concat demuxer 列表文件（绝对路径，单引号转义）。"""
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            esc = os.path.abspath(p).replace("'", "'\\''")
            f.write(f"file '{esc}'\n")


def _run_ffmpeg(cmd, duration, label, progress_cb, cancel_check=None):
    """启动 ffmpeg 并读取进度，返回 bool。"""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        if progress_cb:
            progress_cb(-1, "错误: FFmpeg 未安装")
        return False
    full = [ffmpeg, "-y", "-nostats", "-progress", "pipe:2"] + cmd
    result = run_ffmpeg(full, duration=duration, label=label,
                        cancel_check=cancel_check,
                        progress_callback=progress_cb,
                        translate_error=True)
    if result.success:
        return True
    if result.cancelled:
        return False
    if progress_cb:
        progress_cb(-1, f"失败: {result.error_cn}")
    return False


def _merge_copy(paths, output_path, progress_cb, cancel_check):
    """concat demuxer 流复制合并（要求编码/分辨率一致）。"""
    total = sum(_duration_of(p) for p in paths) or 1.0
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)
    fd, list_path = tempfile.mkstemp(
        prefix="_fm_merge_", suffix=".txt", dir=output_dir)
    os.close(fd)
    try:
        _write_concat_list(paths, list_path)
        args = ["-f", "concat", "-safe", "0", "-i", list_path,
                "-c", "copy", "-map_metadata", "0", output_path]
        return _run_ffmpeg(args, total, "合并中", progress_cb, cancel_check)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def _merge_reencode(paths, output_path, resolution, audio_flags,
                    progress_cb, cancel_check):
    """filter_complex 重编码合并：统一缩放 + 音频规格后 concat。"""
    total = sum(_duration_of(p) for p in paths) or 1.0
    n = len(paths)
    w, h = resolution if resolution else (1920, 1080)
    parts = []
    v_in = []
    a_in = []
    audio_flags = tuple(audio_flags or ())
    preserve_audio = any(audio_flags)
    for i in range(n):
        # 等比缩放并 pad 到目标分辨率（首视频分辨率），保证可拼接
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
        v_in.append(f"[v{i}]")
        if preserve_audio and i < len(audio_flags) and audio_flags[i]:
            parts.append(
                f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                f"channel_layouts=stereo[a{i}]")
            a_in.append(f"[a{i}]")
        elif preserve_audio:
            # 个别输入没有音轨时补等长静音，保留其他视频的声音并保证
            # concat 的每个分段都有一条音频输入。
            duration = max(_duration_of(paths[i]), 0.001)
            parts.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={duration:.6f},asetpts=N/SR/TB[a{i}]")
            a_in.append(f"[a{i}]")
    v_label = "".join(v_in)
    fc = ";".join(parts)
    if preserve_audio:
        a_label = "".join(a_in)
        fc += f";{v_label}concat=n={n}:v=1:a=0[outv];" \
              f"{a_label}concat=n={n}:v=0:a=1[outa]"
        maps = ["-map", "[outv]", "-map", "[outa]"]
    else:
        fc += f";{v_label}concat=n={n}:v=1:a=0[outv]"
        maps = ["-map", "[outv]", "-an"]
    args = []
    for p in paths:
        args += ["-i", os.path.abspath(p)]
    args += ["-filter_complex", fc, *maps,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "192k", "-threads", "0",
             "-map_metadata", "0", output_path]
    return _run_ffmpeg(args, total, "合并中(重编码)", progress_cb, cancel_check)


def merge_videos(paths, output_path, progress_cb=None, cancel_check=None):
    """按列表顺序合并多个视频为单个 MP4。

    同编码/分辨率走 concat demuxer 流复制（快）；否则或失败回退
    filter_complex 重编码（libx264 + aac）。返回是否成功。
    """
    paths = [p for p in (paths or []) if p and os.path.isfile(p)]
    if len(paths) < 2:
        if progress_cb:
            progress_cb(-1, "错误: 合并至少需要 2 个文件")
        return False
    if progress_cb:
        progress_cb(0, f"准备合并 {len(paths)} 个视频…")

    compatible, resolution, audio_flags = _probe_compatible(paths)
    if compatible:
        if _merge_copy(paths, output_path, progress_cb, cancel_check):
            return True
        if progress_cb:
            progress_cb(0, "流复制失败，回退重编码…")
    else:
        if progress_cb:
            progress_cb(0, "编码/分辨率不一致，使用重编码合并…")
    # 编码不一致或流复制失败 → 重编码
    return _merge_reencode(paths, output_path, resolution, audio_flags,
                           progress_cb, cancel_check)


def make_runner(task):
    """TaskManager 持久化重建用的 runner 工厂（runner_key="video_merge"）。

    task.params 需包含 "files"（完整视频路径列表，按合并顺序）。
    """
    def runner(t, prog):
        files = list(t.params.get("files") or [])
        if not files and t.file_path:
            files = [t.file_path]
        return merge_videos(
            files, t.output_path, prog,
            cancel_check=lambda: t.state == "cancelled")
    return runner
