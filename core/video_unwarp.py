"""video_unwarp — 视频反挤压（宽高比修复）。

场景：老视频 / 模拟采集 / 非正方形像素视频在播放时画面被压扁或拉长
（人物变胖变瘦），需要把画面恢复到正确的显示比例。

两种模式：
- 自动修复：按视频自带的 DAR（显示宽高比）修正 SAR 元数据，流复制、快
- 手动反挤压：把画面按目标比例（4:3 / 16:9 / 9:16 / 1:1 / 自定义）拉伸到
  对应像素尺寸，重编码
"""

import os
import tempfile

from core.ffmpeg_executor import get_ffprobe_raw
from core.ffmpeg_progress import run_ffmpeg
from utils.config import get_ffmpeg_path


def get_video_dar(path):
    """读取视频的 (width, height, dar_str, sar_str)；失败返回 None。

    dar_str 如 '16:9' / '4:3' / '0:1'（未知）。
    """
    info = get_ffprobe_raw(path)
    if not info or not info.get("streams"):
        return None
    for s in info["streams"]:
        if s.get("codec_type") == "video":
            w = s.get("width") or 0
            h = s.get("height") or 0
            dar = s.get("display_aspect_ratio", "0:1")
            sar = s.get("sample_aspect_ratio", "1:1")
            return (int(w), int(h), str(dar), str(sar))
    return None


def _run_ffmpeg(args, duration, label, progress_cb, cancel_check):
    """启动 FFmpeg 并等待完成。"""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        if progress_cb:
            progress_cb(-1, "错误: FFmpeg 未安装")
        return False
    result = run_ffmpeg(args, duration=duration, label=label,
                        cancel_check=cancel_check,
                        progress_callback=progress_cb,
                        translate_error=True)
    if result.success:
        return True
    if result.cancelled:
        return False
    if progress_cb:
        progress_cb(-1, f"失败: {result.error_cn or 'FFmpeg 执行失败'}")
    return False


def _parse_dar(dar_str):
    """'16:9' → (16, 9)；非法返回 None。"""
    if not dar_str or ":" not in dar_str:
        return None
    a, b = dar_str.split(":", 1)
    try:
        x, y = int(a), int(b)
    except ValueError:
        return None
    if x <= 0 or y <= 0:
        return None
    return (x, y)


def _target_dimensions(width, height, ratio):
    """计算严格符合目标比例的偶数尺寸，同时尽量保持原长边分辨率。"""
    ratio_w, ratio_h = ratio
    source_ratio = width / height
    target_ratio = ratio_w / ratio_h
    reference = width / ratio_w if source_ratio > target_ratio \
        else height / ratio_h
    # 比例已约分时至少一边为奇数，因此倍率取偶数即可保证两边均为偶数。
    multiplier = max(2, int(round(reference / 2.0) * 2))
    return ratio_w * multiplier, ratio_h * multiplier


def _run_atomic(args, output_path, duration, progress_cb, cancel_check):
    """FFmpeg 成功后才替换目标文件，失败或取消时保留已有结果。"""
    requested_output = output_path
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    suffix = os.path.splitext(output_path)[1] or ".mp4"
    fd, staged_output = tempfile.mkstemp(
        prefix=".formatmaster-unwarp-", suffix=suffix, dir=output_dir)
    os.close(fd)
    try:
        staged_args = [
            staged_output
            if arg == requested_output or (
                isinstance(arg, str) and os.path.abspath(arg) == output_path)
            else arg
            for arg in args
        ]
        if not _run_ffmpeg(staged_args, duration or 1.0, "反挤压修复中",
                           progress_cb, cancel_check):
            return False
        os.replace(staged_output, output_path)
        return True
    finally:
        if os.path.exists(staged_output):
            os.remove(staged_output)


def fix_aspect(input_path, output_path, target="auto",
               progress_cb=None, cancel_check=None):
    """反挤压修复。

    target:
        "auto"            按视频自带 DAR 修正 SAR 元数据（流复制，快）
        "4:3"/"16:9"/"9:16"/"1:1"
                          手动反挤压：拉伸画面到目标比例（重编码）
        自定义 "W:H"      同上，任意比例
    返回 True/False。
    """
    if not os.path.isfile(input_path):
        if progress_cb:
            progress_cb(-1, "错误: 找不到输入视频")
        return False
    if not output_path or os.path.abspath(input_path) == os.path.abspath(
            output_path):
        if progress_cb:
            progress_cb(-1, "错误: 输出路径不能与源视频相同")
        return False
    meta = get_video_dar(input_path)
    if not meta:
        if progress_cb:
            progress_cb(-1, "错误: 无法读取视频信息")
        return False
    width, height, dar_str, sar_str = meta
    if width <= 0 or height <= 0:
        if progress_cb:
            progress_cb(-1, "错误: 视频分辨率无效")
        return False
    duration = _probe_duration(input_path)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        if progress_cb:
            progress_cb(-1, "错误: FFmpeg 未安装")
        return False

    if target == "auto":
        # 自动：用视频自带 DAR 修正 SAR（只改元数据，流复制）
        ratio = _parse_dar(dar_str)
        if not ratio:
            if progress_cb:
                progress_cb(-1, "该视频没有 DAR 信息，请手动选择目标比例")
            return False
        dar = f"{ratio[0]}:{ratio[1]}"
        args = [ffmpeg, "-y", "-i", input_path,
                "-map", "0", "-c", "copy", "-aspect", dar,
                "-map_metadata", "0",
                output_path]
        return _run_atomic(args, output_path, duration,
                           progress_cb, cancel_check)

    # 手动：拉伸到目标比例
    ratio = _parse_dar(target)
    if not ratio:
        if progress_cb:
            progress_cb(-1, "目标比例无效")
        return False
    new_w, new_h = _target_dimensions(width, height, ratio)
    if max(new_w, new_h) > 16384:
        if progress_cb:
            progress_cb(-1, "目标比例会生成超过 16384 像素的画面，请调整比例")
        return False
    square_pixels = _parse_dar(sar_str) == (1, 1)
    if (abs(new_w - width) <= 2 and abs(new_h - height) <= 2
            and square_pixels):
        # 尺寸与像素比例均已正确时无需重复编码。
        args = [ffmpeg, "-y", "-i", input_path,
                "-map", "0", "-c", "copy", "-map_metadata", "0",
                output_path]
        return _run_atomic(args, output_path, duration,
                           progress_cb, cancel_check)
    vf = f"scale={new_w}:{new_h},setsar=1"
    args = [ffmpeg, "-y", "-i", input_path,
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", vf, "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-map_metadata", "0"]
    if os.path.splitext(output_path)[1].lower() in {".mp4", ".mov", ".m4v"}:
        args.extend(["-movflags", "+faststart"])
    args.append(output_path)
    return _run_atomic(args, output_path, duration,
                       progress_cb, cancel_check)


def _probe_duration(path):
    try:
        info = get_ffprobe_raw(path)
        if info and info.get("format"):
            return float(info["format"].get("duration", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0
