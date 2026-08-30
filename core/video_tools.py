"""video_tools — 视频处理工具集（剪辑 / 合并 / 字幕烧录 / 变速 / 去水印）。

统一基于 run_ffmpeg 进度解析，
复用 core/video_converter 的调用范式；不依赖 GUI。
各函数均接受 progress_cb(pct, msg) 进度回调，失败时回调 (-1, 原因)。
"""
import os
import tempfile

from core.ffmpeg_executor import get_ffprobe_info, get_ffprobe_raw
from core.ffmpeg_progress import run_ffmpeg
from utils.config import get_ffmpeg_path


def _duration_of(path):
    """用 ffprobe 获取媒体时长（秒），失败返回 0。"""
    try:
        info = get_ffprobe_info(path)
        if info:
            return float(info.get("duration") or 0.0)
    except Exception:
        pass
    return 0.0


def _has_audio_stream(path):
    """视频是否含音频流（filter_complex 引用 [0:a] 前必须确认）。"""
    try:
        info = get_ffprobe_raw(path)
        if info and info.get("streams"):
            return any(s.get("codec_type") == "audio"
                       for s in info["streams"])
    except Exception:
        pass
    return False


def _run(args, duration, label, progress_cb, cancel_check=None,
         report_error=True):
    """启动 ffmpeg 并读取进度，返回 bool。"""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        if progress_cb:
            progress_cb(-1, "错误: FFmpeg 未安装")
        return False
    cmd = [ffmpeg, "-y", "-nostats", "-progress", "pipe:2"] + args
    result = run_ffmpeg(cmd, duration=duration, label=label,
                        cancel_check=cancel_check,
                        progress_callback=progress_cb,
                        translate_error=True)
    if result.success:
        return True
    if result.cancelled:
        return False
    if progress_cb and report_error:
        progress_cb(-1, f"失败: {result.error_cn}")
    return False


def clip_video(input_path, output_path, start_sec=None, end_sec=None,
               progress_cb=None, cancel_check=None, vf=None):
    """截取视频片段。start_sec/end_sec 为秒数（None 表示不限）。

    流复制优先（快），若失败自动降级为重编码。
    vf: 视频滤镜串（如 "transpose=1,hflip" / "eq=brightness=0.1"），
        非空时直接走重编码路径（流复制无法应用滤镜）。
    """
    duration = _duration_of(input_path)
    try:
        start_sec = max(0.0, float(start_sec or 0.0))
        end_sec = None if end_sec in (None, "") else float(end_sec)
    except (TypeError, ValueError):
        if progress_cb:
            progress_cb(-1, "错误: 剪辑时间格式无效")
        return False
    if end_sec is not None and end_sec <= start_sec:
        if progress_cb:
            progress_cb(-1, "错误: 结束时间必须晚于开始时间")
        return False
    seg = (end_sec if end_sec is not None else duration) - start_sec
    seg = max(seg, 1.0)

    def _seek_args():
        args = []
        if start_sec:
            args += ["-ss", str(start_sec)]
        args += ["-i", input_path]
        if end_sec is not None:
            # 使用片段时长而不是绝对 -to，避免非零起点输出过长。
            args += ["-t", str(end_sec - start_sec)]
        return args

    # 带滤镜 → 必须重编码
    if vf:
        args = _seek_args()
        args += ["-vf", vf, "-c:v", "libx264", "-preset", "fast",
                 "-c:a", "aac", "-map_metadata", "0", output_path]
        return _run(args, seg, "剪辑中", progress_cb, cancel_check)

    args = _seek_args()
    args += ["-c", "copy", "-map_metadata", "0", output_path]
    if _run(args, seg, "剪辑中", progress_cb, cancel_check,
            report_error=False):
        return True
    if cancel_check and cancel_check():
        return False
    # 流复制失败（如非关键帧起点）→ 重编码兜底
    args = _seek_args()
    args += ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
             "-map_metadata", "0", output_path]
    return _run(args, seg, "剪辑中(重编码)", progress_cb, cancel_check)


def merge_videos(input_paths, output_path, progress_cb=None,
                 cancel_check=None):
    """合并多个视频（concat demuxer，自动选用兼容编码）。"""
    if len(input_paths) < 2:
        if progress_cb:
            progress_cb(-1, "错误: 合并至少需要 2 个文件")
        return False
    total = sum(_duration_of(p) for p in input_paths) or 1.0
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)
    fd, list_path = tempfile.mkstemp(
        prefix="_fm_concat_", suffix=".txt", dir=output_dir)
    os.close(fd)
    try:
        # 使用绝对路径，跨目录输入也能稳定解析；单引号按 concat 文件
        # 语法转义，避免带引号的文件名破坏列表。
        content = "".join(
            f"file '{os.path.abspath(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
            for p in input_paths)
        with open(list_path, "w", encoding="utf-8") as f:
            f.write(content)
        args = ["-f", "concat", "-safe", "0", "-i", list_path,
                "-c", "copy", "-map_metadata", "0", output_path]
        ok = _run(args, total, "合并中", progress_cb, cancel_check,
                  report_error=False)
        if not ok:
            if cancel_check and cancel_check():
                return False
            # 编码不一致 → 统一重编码
            args = ["-f", "concat", "-safe", "0", "-i", list_path,
                    "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
                    "-map_metadata", "0", output_path]
            ok = _run(args, total, "合并中(重编码)", progress_cb, cancel_check)
        return ok
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def burn_subtitle(input_path, subtitle_path, output_path,
                  progress_cb=None, cancel_check=None):
    """字幕烧录（软字幕合成进画面，需要重编码）。"""
    if not os.path.isfile(subtitle_path):
        if progress_cb:
            progress_cb(-1, "错误: 字幕文件不存在")
        return False
    duration = _duration_of(input_path) or 1.0
    # subtitles 滤镜：Windows 下路径需转义（: 和 \）
    esc = subtitle_path.replace("\\", "/").replace(":", "\\:")
    args = ["-i", input_path,
            "-vf", f"subtitles='{esc}'",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", "-map_metadata", "0", output_path]
    return _run(args, duration, "烧录字幕中", progress_cb, cancel_check)


def change_speed(input_path, output_path, rate, progress_cb=None,
                 cancel_check=None):
    """视频变速。rate>1 加速，0<rate<1 减速。"""
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    duration = _duration_of(input_path) / rate or 1.0
    # 无音轨视频不能引用 [0:a]（否则 FFmpeg 报流不存在）
    has_audio = _has_audio_stream(input_path)
    if has_audio:
        fc = (f"[0:v]setpts=PTS/{rate}[v];[0:a]atempo={max(rate, 0.5)}[a]"
              if 0.5 <= rate <= 2.0 else
              f"[0:v]setpts=PTS/{rate}[v];[0:a]atempo={rate}[a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = f"[0:v]setpts=PTS/{rate}[v]"
        maps = ["-map", "[v]"]
    args = ["-i", input_path, "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", "-map_metadata", "0", output_path]
    return _run(args, duration, "变速处理中", progress_cb, cancel_check)


def remove_logo(input_path, output_path, x, y, w, h, progress_cb=None,
                cancel_check=None):
    """视频去水印：用 FFmpeg delogo 滤镜对指定区域做插值模糊。

    x/y/w/h 为水印区域（像素坐标与尺寸）。delogo 通过周围像素插值
    模糊覆盖，适合台标/角落水印；全屏大面积水印效果有限。
    区域自动钳制到画面内且不贴边（delogo 需 1px 插值边界）。
    """
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        return False
    # 读取视频分辨率用于钳制区域
    width, height = 0, 0
    try:
        raw = get_ffprobe_raw(input_path, timeout=10)
        if raw and raw.get("streams"):
            for s in raw["streams"]:
                if s.get("codec_type") == "video" and s.get("width"):
                    width, height = int(s["width"]), int(s["height"])
                    break
    except Exception:
        pass
    if width > 0 and height > 0:
        # delogo 不允许区域贴边（内部插值需要边界），钳制到 [1, 尺寸-2]
        x = max(1, min(x, width - 2))
        y = max(1, min(y, height - 2))
        w = min(w, width - x - 2)
        h = min(h, height - y - 2)
    if w <= 0 or h <= 0:
        return False
    duration = _duration_of(input_path) or 1.0
    args = ["-i", input_path,
            "-vf", f"delogo=x={x}:y={y}:w={w}:h={h}:show=0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "copy", "-map_metadata", "0", output_path]
    return _run(args, duration, "去水印处理中", progress_cb, cancel_check)
# ─────────────────────────────────────────────────────
#  扩展滤镜集（2026-08-15 合并进「视频处理」面板）
#  倒放 / 转GIF / 文字水印 / 视频稳定 / 画质增强 /
#  画面裁剪 / 补帧慢动作 / 去隔行 / 音轨替换与混音
# ─────────────────────────────────────────────────────

def _pick_cjk_font():
    """挑一个 drawtext 可用的中文字体文件（系统默认优先）。"""
    import os as _os
    cands = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/PingFang.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for fp in cands:
        try:
            if _os.path.isfile(fp):
                return fp
        except Exception:
            continue
    return None


def reverse_video(input_path, output_path, keep_audio=True,
                  progress_cb=None, cancel_check=None):
    """视频倒放。keep_audio=True 时音轨同步倒放（鬼畜/复盘）。"""
    duration = _duration_of(input_path) or 1.0
    has_audio = _has_audio_stream(input_path) and keep_audio
    if has_audio:
        fc = "[0:v]reverse[v];[0:a]areverse[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = "[0:v]reverse[v]"
        maps = ["-map", "[v]"]
    args = ["-i", input_path, "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac" if has_audio else "copy",
            "-map_metadata", "0", output_path]
    return _run(args, duration, "倒放处理中", progress_cb, cancel_check)


def video_to_gif(input_path, output_path, fps=15, max_width=480,
                 progress_cb=None, cancel_check=None, start_sec=0.0,
                 duration_sec=None):
    """视频转 GIF（palettegen/paletteuse 两遍，高质量调色板）。

    max_width 限制输出宽度（等比缩放），控制体积。
    """
    source_duration = _duration_of(input_path) or 1.0
    try:
        fps = max(1, int(fps))
        max_width = None if max_width in (None, "", 0, "0") else max(32, int(max_width))
        start_sec = max(0.0, float(start_sec or 0.0))
        duration_sec = (None if duration_sec in (None, "")
                        else max(0.2, float(duration_sec)))
    except (TypeError, ValueError):
        fps, max_width, start_sec, duration_sec = 15, 480, 0.0, None
    remaining = max(0.2, source_duration - start_sec)
    segment_duration = min(remaining, duration_sec) if duration_sec else remaining
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)
    # 每个任务使用独立调色板，避免同目录并发转换互相覆盖。
    fd, pal = tempfile.mkstemp(
        prefix="_fm_gif_palette_", suffix=".png", dir=output_dir)
    os.close(fd)
    try:
        os.remove(pal)  # palettegen 需要创建输出文件
    except OSError:
        pass

    def _input_args():
        args = []
        if start_sec:
            args += ["-ss", str(start_sec)]
        args += ["-i", input_path]
        return args

    def _stage_progress(offset, span):
        if progress_cb is None:
            return None

        def _progress(pct, message):
            if pct < 0:
                progress_cb(pct, message)
            else:
                progress_cb(min(100, offset + int(pct * span / 100)), message)
        return _progress

    try:
        # 第一遍：生成调色板
        scale = (f"scale={max_width}:-1:flags=lanczos"
                 if max_width else "scale=-1:-1")
        args1 = [*_input_args()]
        if duration_sec:
            args1 += ["-t", str(segment_duration)]
        args1 += ["-vf",
                 f"fps={fps},{scale},palettegen=max_colors=256",
                 "-y", pal]
        ok = _run(args1, segment_duration, "生成调色板…",
                  _stage_progress(0, 45), cancel_check)
        if not ok:
            return False
        # 第二遍：用调色板输出 GIF
        args2 = [*_input_args(), "-i", pal]
        if duration_sec:
            args2 += ["-t", str(segment_duration)]
        args2 += ["-lavfi",
                 f"fps={fps},{scale}[x];[x][1:v]paletteuse=dither=bayer",
                 "-y", output_path]
        return _run(args2, segment_duration, "生成 GIF…",
                    _stage_progress(45, 55), cancel_check)
    finally:
        try:
            os.remove(pal)
        except OSError:
            pass


def burn_text_watermark(input_path, output_path, text, font_size=48,
                        position="bottom_right", opacity=0.7,
                        progress_cb=None, cancel_check=None):
    """视频文字水印：Pillow 生成透明图层，再由 FFmpeg overlay 合成。

    不依赖 FFmpeg 的可选 drawtext/libfreetype 编译能力；发布版和常见系统
    FFmpeg 即使没有 drawtext，也能稳定完成中文水印处理。
    """
    if not text:
        if progress_cb:
            progress_cb(-1, "错误: 水印文字不能为空")
        return False
    font = _pick_cjk_font()
    duration = _duration_of(input_path) or 1.0
    alpha = max(0.0, min(1.0, float(opacity)))
    output_dir = os.path.dirname(output_path) or "."
    fd, overlay_path = tempfile.mkstemp(
        prefix="_fm_text_watermark_", suffix=".png", dir=output_dir)
    os.close(fd)
    try:
        from PIL import Image, ImageDraw, ImageFont

        size = max(8, int(font_size))
        try:
            font_obj = ImageFont.truetype(font, size) if font else \
                ImageFont.load_default(size=size)
        except (OSError, TypeError):
            font_obj = ImageFont.load_default()
        probe = Image.new("RGBA", (1, 1))
        bounds = ImageDraw.Draw(probe).textbbox(
            (0, 0), str(text), font=font_obj, stroke_width=2)
        width = max(1, bounds[2] - bounds[0] + 8)
        height = max(1, bounds[3] - bounds[1] + 8)
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.text(
            (4 - bounds[0], 4 - bounds[1]), str(text), font=font_obj,
            fill=(255, 255, 255, round(255 * alpha)),
            stroke_width=2, stroke_fill=(0, 0, 0, round(128 * alpha)))
        layer.save(overlay_path, "PNG")

        pos = {
            "top_left": "x=24:y=24",
            "top_right": "x=W-w-24:y=24",
            "bottom_left": "x=24:y=H-h-24",
            "bottom_right": "x=W-w-24:y=H-h-24",
            "center": "x=(W-w)/2:y=(H-h)/2",
        }.get(position, "x=W-w-24:y=H-h-24")
        args = ["-i", input_path, "-i", overlay_path,
                "-filter_complex", f"[0:v][1:v]overlay={pos}",
                "-c:v", "libx264", "-preset", "fast",
                "-c:a", "copy", "-map_metadata", "0", output_path]
        return _run(
            args, duration, "添加文字水印…", progress_cb, cancel_check)
    except Exception as exc:  # noqa: BLE001 - 图层生成失败转为业务失败
        if progress_cb:
            progress_cb(-1, f"错误: 文字水印生成失败（{exc}）")
        return False
    finally:
        try:
            os.remove(overlay_path)
        except OSError:
            pass


def stabilize_video(input_path, output_path,
                    progress_cb=None, cancel_check=None):
    """视频稳定（deshake，修复手持抖动）。"""
    duration = _duration_of(input_path) or 1.0
    args = ["-i", input_path, "-vf", "deshake",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", "-map_metadata", "0", output_path]
    return _run(args, duration, "视频稳定中…", progress_cb, cancel_check)


def enhance_video(input_path, output_path, sharpen=0, denoise=0,
                  progress_cb=None, cancel_check=None):
    """画质增强：unsharp 锐化 + hqdn3d 降噪（0 表示关闭）。"""
    duration = _duration_of(input_path) or 1.0
    filters = []
    try:
        sharpen = float(sharpen)
    except (TypeError, ValueError):
        sharpen = 0.0
    try:
        denoise = float(denoise)
    except (TypeError, ValueError):
        denoise = 0.0
    if sharpen > 0:
        luma = min(5.0, sharpen)
        filters.append(f"unsharp=5:5:{luma:.2f}:5:5:0.0")
    if denoise > 0:
        strength = min(8.0, denoise)
        filters.append(f"hqdn3d={strength:.1f}:{strength:.1f}:{strength:.1f}:{strength:.1f}")
    if not filters:
        if progress_cb:
            progress_cb(-1, "错误: 锐化与降噪均为 0")
        return False
    args = ["-i", input_path, "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "copy", "-map_metadata", "0", output_path]
    return _run(args, duration, "画质增强中…", progress_cb, cancel_check)


def crop_video(input_path, output_path, x, y, w, h,
               progress_cb=None, cancel_check=None):
    """画面裁剪（crop）。x/y/w/h 像素坐标，自动钳制到画面内。"""
    try:
        x, y, w, h = int(x), int(y), int(w), int(h)
    except (TypeError, ValueError):
        if progress_cb:
            progress_cb(-1, "错误: 裁剪参数无效")
        return False
    # 读取分辨率钳制
    width, height = 0, 0
    try:
        raw = get_ffprobe_raw(input_path, timeout=10)
        if raw and raw.get("streams"):
            for s in raw["streams"]:
                if s.get("codec_type") == "video" and s.get("width"):
                    width, height = int(s["width"]), int(s["height"])
                    break
    except Exception:
        pass
    if width > 0:
        x = max(0, min(x, width - 1))
        w = min(w, width - x)
    if height > 0:
        y = max(0, min(y, height - 1))
        h = min(h, height - y)
    if w <= 0 or h <= 0:
        if progress_cb:
            progress_cb(-1, "错误: 裁剪区域无效")
        return False
    duration = _duration_of(input_path) or 1.0
    args = ["-i", input_path, "-vf", f"crop={w}:{h}:{x}:{y}",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "copy", "-map_metadata", "0", output_path]
    return _run(args, duration, "画面裁剪中…", progress_cb, cancel_check)


def slowmo_interp(input_path, output_path, rate, target_fps=60,
                  progress_cb=None, cancel_check=None):
    """补帧慢动作（minterpolate 运动补偿，0<rate<1 慢放且丝滑）。"""
    try:
        rate = float(rate)
        target_fps = max(1, int(target_fps))
    except (TypeError, ValueError):
        rate, target_fps = 0.5, 60
    if not (0 < rate < 1):
        if progress_cb:
            progress_cb(-1, "错误: 补帧慢动作需 0<倍速<1")
        return False
    duration = _duration_of(input_path) / rate or 1.0
    has_audio = _has_audio_stream(input_path)
    fc = (f"[0:v]setpts=PTS/{rate},minterpolate=fps={target_fps}:"
          f"mi_mode=mci:mc_mode=aobmc:vsync=vfr[v]")
    if has_audio:
        fc += f";[0:a]atempo={rate:.4f}[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        maps = ["-map", "[v]"]
    args = ["-i", input_path, "-filter_complex", fc, *maps,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac" if has_audio else "copy",
            "-map_metadata", "0", output_path]
    return _run(args, duration, "补帧慢动作…", progress_cb, cancel_check)


def deinterlace_video(input_path, output_path,
                      progress_cb=None, cancel_check=None):
    """去隔行扫描（yadif，老电视/DV 视频修复）。"""
    duration = _duration_of(input_path) or 1.0
    args = ["-i", input_path, "-vf", "yadif=mode=1",
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "copy", "-map_metadata", "0", output_path]
    return _run(args, duration, "去隔行处理中…", progress_cb, cancel_check)


def replace_audio(input_path, audio_path, output_path, keep_video_codec=True,
                  progress_cb=None, cancel_check=None):
    """音轨替换：视频画面 + 新的音轨（-map 0:v -map 1:a）。"""
    if not os.path.isfile(audio_path):
        if progress_cb:
            progress_cb(-1, "错误: 音频文件不存在")
        return False
    duration = _duration_of(input_path) or 1.0
    args = ["-i", input_path, "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy" if keep_video_codec else "libx264",
            "-c:a", "aac", "-shortest", "-map_metadata", "0", output_path]
    return _run(args, duration, "替换音轨中…", progress_cb, cancel_check)


def mix_audio(input_path, audio_path, output_path, bg_volume=0.3,
              progress_cb=None, cancel_check=None):
    """音轨混音：原声 + 背景音乐 amix（可调背景音量比例）。"""
    if not os.path.isfile(audio_path):
        if progress_cb:
            progress_cb(-1, "错误: 音频文件不存在")
        return False
    try:
        bg_volume = max(0.0, min(1.0, float(bg_volume)))
    except (TypeError, ValueError):
        bg_volume = 0.3
    # 没有原音轨时，“混音”自然退化为添加背景音；不能引用不存在的
    # [0:a]，否则 FFmpeg 会直接报流不存在。
    if not _has_audio_stream(input_path):
        return replace_audio(
            input_path, audio_path, output_path,
            progress_cb=progress_cb, cancel_check=cancel_check)
    duration = _duration_of(input_path) or 1.0
    main_v = max(0.0, min(1.0, 1.0 - bg_volume))
    fc = (f"[0:a]volume={main_v:.2f}[a0];"
          f"[1:a]volume={bg_volume:.2f}[a1];"
          f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]")
    args = ["-i", input_path, "-i", audio_path,
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-map_metadata", "0", output_path]
    return _run(args, duration, "混音处理中…", progress_cb, cancel_check)
