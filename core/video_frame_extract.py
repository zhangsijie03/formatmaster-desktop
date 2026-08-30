# -*- coding: utf-8 -*-
"""视频抽帧 / 场景截图：按固定间隔批量截取视频关键帧。"""
import os
import math
import shutil
import tempfile

from core.ffmpeg_progress import run_ffmpeg
from utils.config import get_ffmpeg_path
from core.ffmpeg_executor import get_ffprobe_raw

_SUPPORTED = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
              ".ts", ".m4v", ".mpg", ".mpeg", ".3gp"}
_FRAME_EXTS = (".png", ".jpg", ".jpeg")


def _is_generated_frame(name):
    return name.lower().startswith("frame_") and name.lower().endswith(
        _FRAME_EXTS)


def duration_of(path):
    """视频时长（秒），失败返回 0。"""
    try:
        info = get_ffprobe_raw(path, timeout=10)
        if info and "format" in info:
            return float(info["format"].get("duration", 0) or 0)
    except Exception:
        pass
    return 0


def extract_frames(input_path, output_dir, interval_sec=1.0, fmt="PNG",
                   progress_cb=None, cancel_check=None):
    """按间隔抽帧到 output_dir，返回 (成功, 帧数)。

    progress_cb(pct, msg)：0~100 进度回调。
    cancel_check() -> bool：返回 True 时中断（抛 InterruptedError）。
    """
    if not os.path.isfile(input_path) or os.path.splitext(input_path)[1].lower() \
            not in _SUPPORTED:
        return False, 0
    try:
        interval_sec = float(interval_sec)
    except (TypeError, ValueError):
        return False, 0
    if not math.isfinite(interval_sec) or interval_sec <= 0:
        return False, 0
    fmt = str(fmt).upper()
    if fmt not in ("PNG", "JPG", "JPEG"):
        return False, 0
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, 0
    output_dir = os.path.abspath(output_dir)
    if os.path.isfile(output_dir):
        return False, 0
    parent_dir = os.path.dirname(output_dir)
    os.makedirs(parent_dir, exist_ok=True)
    duration = duration_of(input_path) or 0
    if duration <= 0:
        duration = 0  # 未知时长时按 100% 上报（仅 ffmpeg 侧结束）

    # 先写入同盘临时目录；FFmpeg 失败或用户取消时保留上一版完整结果，
    # 避免页面显示成功目录却只剩一批半成品。
    stage_dir = tempfile.mkdtemp(prefix=".formatmaster-frames-",
                                 dir=parent_dir)
    ext = "jpg" if fmt in ("JPG", "JPEG") else "png"
    fps = max(0.01, 1.0 / max(interval_sec, 0.1))
    out_tpl = os.path.join(stage_dir, "frame_%05d." + ext)
    cmd = [ffmpeg, "-y", "-i", input_path, "-vf", f"fps={fps}",
           "-start_number", "0", "-q:v", "2" if ext == "jpg" else "1",
           out_tpl]
    try:
        result = run_ffmpeg(cmd, duration=duration, label="抽帧中",
                            cancel_check=cancel_check,
                            progress_callback=progress_cb,
                            translate_error=True)
        if result.cancelled:
            raise InterruptedError("已取消")
        staged = sorted(f for f in os.listdir(stage_dir)
                        if _is_generated_frame(f))
        if not result.success or not staged:
            if progress_cb and result.error_cn:
                progress_cb(-1, result.error_cn)
            return False, 0

        os.makedirs(output_dir, exist_ok=True)
        backup_dir = os.path.join(stage_dir, "previous")
        os.makedirs(backup_dir)
        previous = [f for f in os.listdir(output_dir)
                    if _is_generated_frame(f)]
        moved = []
        try:
            for name in previous:
                os.replace(os.path.join(output_dir, name),
                           os.path.join(backup_dir, name))
            for name in staged:
                os.replace(os.path.join(stage_dir, name),
                           os.path.join(output_dir, name))
                moved.append(name)
        except OSError:
            for name in moved:
                try:
                    os.remove(os.path.join(output_dir, name))
                except OSError:
                    pass
            for name in previous:
                backup = os.path.join(backup_dir, name)
                if os.path.exists(backup):
                    os.replace(backup, os.path.join(output_dir, name))
            raise
        return True, len(staged)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def extract_strip_frames(input_path, output_dir, n=12):
    """按时间均匀抽 n 帧（胶片条用），返回实际帧数。

    ffmpeg fps 滤镜按 n/时长 取帧率，一次进程抽完全部，快于逐点 -ss。
    返回 (成功, 帧数)；失败返回 (False, 0)。
    """
    if not os.path.isfile(input_path) or n < 2:
        return False, 0
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, 0
    os.makedirs(output_dir, exist_ok=True)
    duration = duration_of(input_path)
    if duration <= 0:
        return False, 0
    for f in os.listdir(output_dir):
        if f.lower().startswith("strip_") and f.lower().endswith(
                (".png", ".jpg", ".jpeg")):
            try:
                os.remove(os.path.join(output_dir, f))
            except OSError:
                pass
    fps = n / duration
    out_tpl = os.path.join(output_dir, "strip_%03d.jpg")
    cmd = [ffmpeg, "-y", "-i", input_path, "-vf", f"fps={fps}",
           "-frames:v", str(n), "-q:v", "3", out_tpl]
    result = run_ffmpeg(cmd, duration=duration, label="生成胶片",
                        translate_error=True)
    if not result.success:
        return False, 0
    count = 0
    for f in sorted(os.listdir(output_dir)):
        if f.lower().startswith("strip_") and f.lower().endswith(
                (".png", ".jpg", ".jpeg")):
            count += 1
    return True, count
