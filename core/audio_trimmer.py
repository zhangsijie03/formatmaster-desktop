"""音频裁剪工具 — 基于 FFmpeg

支持按起止时间裁剪、淡入淡出、波形数据提取（简易振幅包络）。
"""
import math
import os
import subprocess
import struct
import tempfile

from utils.config import get_ffmpeg_path
from core.ffmpeg_progress import run_ffmpeg
from core.ffmpeg_executor import get_ffprobe_raw


def get_audio_duration(filepath):
    """获取音频时长（秒），失败返回 0。"""
    info = get_ffprobe_raw(filepath, timeout=10)
    if info and "format" in info:
        return float(info["format"].get("duration", 0))
    return 0


def get_audio_info(filepath):
    """获取音频基本信息，返回 dict 或 None。

    包含: duration, sample_rate, channels, codec, bitrate
    """
    data = get_ffprobe_raw(filepath, timeout=10)
    if not data:
        return None

    try:
        fmt = data.get("format", {})
        audio_stream = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                audio_stream = s
                break

        return {
            "duration": float(fmt.get("duration", 0)),
            "codec": audio_stream.get("codec_name", "-") if audio_stream else "-",
            "sample_rate": audio_stream.get("sample_rate", "-") if audio_stream else "-",
            "channels": audio_stream.get("channels", "-") if audio_stream else "-",
            "bitrate": fmt.get("bit_rate", "-"),
        }
    except Exception:
        return None


def get_waveform_data(filepath, num_points=500):
    """提取音频简易振幅包络数据，用于波形图绘制。

    通过 FFmpeg 将音频解码为 16-bit 单声道 PCM（采样率 8kHz 以加速），
    然后等分为 num_points 个区间，取每区间最大绝对值作为振幅。

    返回 list[float]，值域 [0.0, 1.0]。失败返回空列表。
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return []
    try:
        num_points = max(1, int(num_points))
    except (TypeError, ValueError, OverflowError):
        return []

    proc = None
    try:
        cmd = [ffmpeg, "-y", "-i", filepath,
               "-f", "s16le", "-acodec", "pcm_s16le",
               "-ar", "8000", "-ac", "1",
               "-v", "quiet",
               "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
        raw, _ = proc.communicate(timeout=30)

        if proc.returncode != 0 or len(raw) < 2:
            return []

        # 将原始 PCM 数据转为 16-bit signed int 数组
        num_samples = len(raw) // 2
        samples = struct.unpack(f'<{num_samples}h', raw[:num_samples * 2])

        # 等分为 num_points 个区间，取每区间最大绝对值
        chunk_size = max(1, num_samples // num_points)
        waveform = []
        max_val = 32768.0

        for i in range(num_points):
            start = i * chunk_size
            end = min(start + chunk_size, num_samples)
            if start >= num_samples:
                waveform.append(0.0)
                continue
            chunk_max = max(abs(s) for s in samples[start:end])
            waveform.append(chunk_max / max_val)

        return waveform
    except subprocess.TimeoutExpired:
        # 超时后必须回收 FFmpeg 子进程，避免连续刷新波形留下后台进程。
        if proc is not None:
            proc.kill()
            proc.communicate()
        return []
    except (OSError, ValueError, struct.error):
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        return []


def trim_audio(input_path, output_path, start_sec=0, end_sec=0,
               fade_in=0, fade_out=0, progress_cb=None,
               cancel_check=None):
    """裁剪音频片段。

    参数:
      start_sec: 起始秒数
      end_sec: 结束秒数（0 表示到文件末尾）
      fade_in: 淡入时长（秒）
      fade_out: 淡出时长（秒）

    返回 bool。
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        if progress_cb:
            progress_cb(-1, "错误：FFmpeg 未安装")
        return False

    if not os.path.isfile(input_path):
        if progress_cb:
            progress_cb(-1, f"错误：找不到文件 {os.path.basename(input_path)}")
        return False

    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(
            os.path.abspath(output_path)):
        if progress_cb:
            progress_cb(-1, "错误：输出文件不能覆盖源文件")
        return False

    try:
        start_sec = float(start_sec)
        end_sec = float(end_sec)
        fade_in = float(fade_in)
        fade_out = float(fade_out)
    except (TypeError, ValueError, OverflowError):
        if progress_cb:
            progress_cb(-1, "错误：裁剪时间参数无效")
        return False
    if (not all(math.isfinite(v) for v in
                (start_sec, end_sec, fade_in, fade_out))
            or start_sec < 0 or fade_in < 0 or fade_out < 0):
        if progress_cb:
            progress_cb(-1, "错误：时间参数不能为负数或非有限值")
        return False

    duration = get_audio_duration(input_path)
    if duration <= 0 or not math.isfinite(duration):
        if progress_cb:
            progress_cb(-1, "错误：无法读取音频时长")
        return False
    if end_sec <= 0:
        end_sec = duration
    else:
        end_sec = min(end_sec, duration)

    if start_sec >= duration or start_sec >= end_sec:
        if progress_cb:
            progress_cb(-1, "错误：开始时间不能大于等于结束时间")
        return False

    clip_duration = end_sec - start_sec
    if fade_in > clip_duration or fade_out > clip_duration:
        if progress_cb:
            progress_cb(-1, "错误：淡入或淡出时长不能超过裁剪片段")
        return False

    if progress_cb:
        progress_cb(10, "开始裁剪...")

    cmd = [ffmpeg, "-y"]

    # 输入定位（-ss 放在 -i 前面加速 seek）
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]

    cmd += ["-i", input_path]

    # 时长
    if end_sec > 0 and end_sec > start_sec:
        cmd += ["-t", str(clip_duration)]

    # 音频滤镜链
    filters = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in}")
    if fade_out > 0 and clip_duration > 0:
        fade_start = max(0, clip_duration - fade_out)
        filters.append(f"afade=t=out:st={fade_start}:d={fade_out}")

    if filters:
        cmd += ["-af", ",".join(filters)]

    output_dir = os.path.dirname(os.path.abspath(output_path))
    try:
        os.makedirs(output_dir, exist_ok=True)
        fd, staged_path = tempfile.mkstemp(
            prefix=".fm_audio_trim_", suffix=os.path.splitext(output_path)[1],
            dir=output_dir)
        os.close(fd)
        os.remove(staged_path)
    except OSError as exc:
        if progress_cb:
            progress_cb(-1, f"错误：无法创建临时输出文件（{exc}）")
        return False

    cmd += ["-threads", "0", staged_path]

    try:
        result = run_ffmpeg(cmd, duration=clip_duration, label="裁剪中",
                            cancel_check=cancel_check,
                            progress_callback=progress_cb,
                            translate_error=True)
        if result.success and os.path.isfile(staged_path):
            # 成功后再原子替换，失败/取消不会破坏已有输出文件。
            os.replace(staged_path, output_path)
            return True
        if result.cancelled:
            return False
        if progress_cb:
            progress_cb(-1, f"失败: {result.error_cn or 'FFmpeg 未生成有效输出'}")
        return False
    except InterruptedError:
        raise
    except Exception as e:
        if progress_cb:
            progress_cb(-1, f"错误：{e}")
        return False
    finally:
        try:
            if os.path.exists(staged_path):
                os.remove(staged_path)
        except OSError:
            pass
