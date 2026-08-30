"""图片批量水印 — 文字水印 + 图片水印

支持：字体/字号/颜色/透明度/旋转角度/位置，以及 PNG 透明图叠加。
纯 Pillow 实现，无额外依赖。
"""
import os
import math
import tempfile
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

# 水印位置映射（x_ratio, y_ratio）
POSITIONS = {
    "top_left": "top_left",
    "top_right": "top_right",
    "bottom_left": "bottom_left",
    "bottom_right": "bottom_right",
    "center": "center",
    "左上角": "top_left", "Top left": "top_left",
    "右上角": "top_right", "Top right": "top_right",
    "左下角": "bottom_left", "Bottom left": "bottom_left",
    "右下角": "bottom_right", "Bottom right": "bottom_right",
    "居中": "center", "Center": "center",
}

_OUTPUT_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
    ".webp": "WEBP", ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
}

# 常见中文字体路径：Windows 优先，macOS 提供 PingFang/宋体等系统字体回退。
_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "C:/Windows/Fonts/simsun.ttc",     # 宋体
    "C:/Windows/Fonts/arial.ttf",      # Arial
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/PingFang.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _get_font(size: int, font_path: str = "") -> ImageFont.FreeTypeFont:
    """获取字体对象，优先用指定路径，否则尝试系统字体，最后回退默认。"""
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    """#RRGGBB → (R, G, B, A)"""
    h = str(hex_color or "").lstrip("#")
    if len(h) == 6:
        try:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return (r, g, b, alpha)
        except ValueError:
            pass
    return (255, 255, 255, alpha)


def _calc_position(img_w: int, img_h: int, wm_w: int, wm_h: int, pos: str) -> Tuple[int, int]:
    """根据位置名称计算水印左上角坐标。"""
    key = POSITIONS.get(pos, "bottom_right")
    margin_x = max(0, round(img_w * 0.02))
    margin_y = max(0, round(img_h * 0.02))
    if key == "top_left":
        x, y = margin_x, margin_y
    elif key == "top_right":
        x, y = img_w - wm_w - margin_x, margin_y
    elif key == "bottom_left":
        x, y = margin_x, img_h - wm_h - margin_y
    elif key == "center":
        x, y = (img_w - wm_w) // 2, (img_h - wm_h) // 2
    else:
        x, y = img_w - wm_w - margin_x, img_h - wm_h - margin_y
    return max(0, x), max(0, y)


def add_text_watermark(
    img: Image.Image,
    text: str,
    font_size: int = 48,
    color: str = "#FFFFFF",
    opacity: float = 0.8,
    rotation: int = 0,
    position: str = "右下角",
    font_path: str = "",
) -> Image.Image:
    """在图片上添加文字水印，返回新 Image 对象。"""
    if not text:
        return img

    base = img if img.mode == "RGBA" else img.convert("RGBA")

    # 创建水印透明层
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font_size = max(1, min(1024, int(font_size)))
    except (TypeError, ValueError, OverflowError):
        font_size = 48
    try:
        opacity = float(opacity)
    except (TypeError, ValueError, OverflowError):
        opacity = 0.8
    if not math.isfinite(opacity):
        opacity = 0.8
    font = _get_font(font_size, font_path)
    alpha = int(255 * max(0, min(1, opacity)))
    fill = _hex_to_rgba(color, alpha)

    # 测量文字大小
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # 先在临时层上画文字，再旋转
    try:
        rotation = int(rotation) % 360
    except (TypeError, ValueError, OverflowError):
        rotation = 0
    if rotation != 0:
        tmp = Image.new("RGBA", (tw + 20, th + 20), (0, 0, 0, 0))
        tmp_draw = ImageDraw.Draw(tmp)
        tmp_draw.text((10 - bbox[0], 10 - bbox[1]), text, fill=fill, font=font)
        rotated_tmp = tmp.rotate(rotation, expand=True, resample=Image.BICUBIC)
        tmp.close()
        tmp = rotated_tmp
        x, y = _calc_position(base.size[0], base.size[1], tmp.size[0], tmp.size[1], position)
        overlay.paste(tmp, (x, y), tmp)
        tmp.close()
    else:
        x, y = _calc_position(base.size[0], base.size[1], tw, th, position)
        draw.text((x - bbox[0], y - bbox[1]), text, fill=fill, font=font)

    result = Image.alpha_composite(base, overlay)
    overlay.close()
    if base is not img:
        base.close()
    return result


def add_image_watermark(
    img: Image.Image,
    watermark_path: str,
    scale: float = 0.2,
    opacity: float = 0.8,
    rotation: int = 0,
    position: str = "右下角",
) -> Image.Image:
    """在图片上叠加 PNG 透明图片水印。"""
    if not watermark_path or not os.path.isfile(watermark_path):
        raise ValueError("水印图片不存在")

    try:
        with Image.open(watermark_path) as _f:
            wm = _f.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise ValueError("无法打开水印图片") from exc

    try:
        scale = float(scale)
        opacity = float(opacity)
    except (TypeError, ValueError, OverflowError):
        wm.close()
        raise ValueError("水印缩放或透明度参数无效") from None
    if not math.isfinite(scale) or not math.isfinite(opacity) or scale <= 0:
        wm.close()
        raise ValueError("水印缩放或透明度参数无效")
    scale = min(2.0, scale)
    opacity = max(0.0, min(1.0, opacity))
    base = img if img.mode == "RGBA" else img.convert("RGBA")

    # 按比例缩放水印
    base_w = base.size[0]
    new_w = max(1, int(base_w * scale))
    ratio = new_w / wm.size[0]
    new_h = max(1, int(wm.size[1] * ratio))
    resized = wm.resize((new_w, new_h), Image.LANCZOS)
    wm.close()
    wm = resized

    # 调整透明度
    if opacity < 1.0:
        alpha = wm.getchannel("A")
        enhanced_alpha = ImageEnhance.Brightness(alpha).enhance(opacity)
        alpha.close()
        wm.putalpha(enhanced_alpha)
        enhanced_alpha.close()

    # 旋转
    try:
        rotation = int(rotation) % 360
    except (TypeError, ValueError, OverflowError):
        rotation = 0
    if rotation != 0:
        rotated = wm.rotate(rotation, expand=True, resample=Image.BICUBIC)
        wm.close()
        wm = rotated

    # 创建透明层并粘贴水印
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    x, y = _calc_position(base.size[0], base.size[1], wm.size[0], wm.size[1], position)
    overlay.paste(wm, (x, y), wm)

    result = Image.alpha_composite(base, overlay)
    overlay.close()
    wm.close()
    if base is not img:
        base.close()
    return result


def process_watermark(
    input_path: str,
    output_path: str,
    wm_type: str = "text",
    text: str = "",
    font_size: int = 48,
    color: str = "#FFFFFF",
    opacity: float = 0.8,
    rotation: int = 0,
    position: str = "右下角",
    font_path: str = "",
    wm_image_path: str = "",
    scale: float = 0.2,
    progress_cb=None,
) -> bool:
    """处理单个文件的水印添加。"""
    if not os.path.isfile(input_path):
        if progress_cb:
            progress_cb(-1, "错误：找不到源图片")
        return False
    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(
            os.path.abspath(output_path)):
        if progress_cb:
            progress_cb(-1, "错误：输出文件不能覆盖源文件")
        return False
    if wm_type not in ("text", "image"):
        if progress_cb:
            progress_cb(-1, "错误：不支持的水印类型")
        return False
    if wm_type == "text" and not str(text or "").strip():
        if progress_cb:
            progress_cb(-1, "错误：水印文字不能为空")
        return False
    if wm_type == "image" and not os.path.isfile(wm_image_path):
        if progress_cb:
            progress_cb(-1, "错误：水印图片不存在")
        return False
    ext = os.path.splitext(output_path)[1].lower()
    output_format = _OUTPUT_FORMATS.get(ext)
    if output_format is None:
        if progress_cb:
            progress_cb(-1, "错误：不支持该输出图片格式")
        return False

    if progress_cb:
        progress_cb(20, "打开图片...")
    try:
        with Image.open(input_path) as source:
            source_info = dict(source.info)
            img = ImageOps.exif_transpose(source).copy()
    except (OSError, ValueError) as e:
        if progress_cb:
            progress_cb(-1, f"错误：无法打开图片 - {e}")
        return False

    staged_path = ""
    try:
        if progress_cb:
            progress_cb(40, "添加水印...")
        original = img
        if wm_type == "text":
            img = add_text_watermark(
                img, str(text), font_size, color, opacity, rotation, position, font_path)
        else:
            img = add_image_watermark(
                img, wm_image_path, scale, opacity, rotation, position)
        if img is not original:
            original.close()

        if progress_cb:
            progress_cb(80, "保存...")

        if ext in (".jpg", ".jpeg", ".bmp"):
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.getchannel("A"))
                img.close()
                img = bg
            elif img.mode not in ("RGB", "L"):
                converted = img.convert("RGB")
                img.close()
                img = converted

        output_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(output_dir, exist_ok=True)
        fd, staged_path = tempfile.mkstemp(
            prefix=".fm_watermark_", suffix=ext, dir=output_dir)
        os.close(fd)
        os.remove(staged_path)

        save_kwargs = {}
        if ext in (".jpg", ".jpeg"):
            save_kwargs = {"quality": 95, "optimize": True}
        elif ext == ".png":
            save_kwargs = {"optimize": True}
        elif ext == ".webp":
            save_kwargs = {"quality": 95, "method": 6}
        elif ext in (".tif", ".tiff"):
            save_kwargs = {"compression": "tiff_lzw"}
        if source_info.get("icc_profile") and ext != ".bmp":
            save_kwargs["icc_profile"] = source_info["icc_profile"]
        img.save(staged_path, format=output_format, **save_kwargs)
        if not os.path.isfile(staged_path) or os.path.getsize(staged_path) <= 0:
            raise OSError("输出文件未生成")
        os.replace(staged_path, output_path)
    except InterruptedError:
        raise
    except (OSError, ValueError) as e:
        if progress_cb:
            progress_cb(-1, f"错误：处理失败 - {e}")
        return False
    finally:
        img.close()
        try:
            if staged_path and os.path.exists(staged_path):
                os.remove(staged_path)
        except OSError:
            pass

    if progress_cb:
        progress_cb(100, "水印添加完成")
    return True


def batch_watermark(
    files: List[str],
    output_dir: str,
    wm_type: str = "text",
    text: str = "",
    font_size: int = 48,
    color: str = "#FFFFFF",
    opacity: float = 0.8,
    rotation: int = 0,
    position: str = "右下角",
    font_path: str = "",
    wm_image_path: str = "",
    scale: float = 0.2,
    progress_cb=None,
) -> int:
    """批量添加水印，返回成功数量。"""
    os.makedirs(output_dir, exist_ok=True)
    total = len(files)
    success = 0
    for i, fp in enumerate(files):
        if progress_cb:
            progress_cb(int(i * 90 / max(total, 1)), f"处理 {i+1}/{total}...")
        name = os.path.splitext(os.path.basename(fp))[0]
        ext = os.path.splitext(fp)[1]
        out = os.path.join(output_dir, f"{name}_watermark{ext}")
        if process_watermark(fp, out, wm_type, text, font_size, color, opacity,
                             rotation, position, font_path, wm_image_path, scale):
            success += 1
    if progress_cb:
        progress_cb(100, f"完成 {success}/{total}")
    return success


def generate_preview(
    input_path: str,
    wm_type: str = "text",
    text: str = "",
    font_size: int = 48,
    color: str = "#FFFFFF",
    opacity: float = 0.8,
    rotation: int = 0,
    position: str = "右下角",
    font_path: str = "",
    wm_image_path: str = "",
    scale: float = 0.2,
    max_preview_size: int = 400,
) -> Optional[Image.Image]:
    """生成预览图（缩小后添加水印），返回 PIL Image 对象。"""
    try:
        with Image.open(input_path) as _f:
            img = _f.copy()
    except Exception:
        return None

    orig_w, orig_h = img.size
    # 缩小到预览尺寸
    if max(orig_w, orig_h) > max_preview_size:
        ratio = max_preview_size / max(orig_w, orig_h)
        img = img.resize((int(orig_w * ratio), int(orig_h * ratio)), Image.LANCZOS)

    if wm_type == "text":
        # 预览时按比例缩放字号
        preview_font = max(10, int(font_size * img.size[0] / max(orig_w, 1)))
        img = add_text_watermark(img, text, preview_font, color, opacity, rotation, position, font_path)
    elif wm_type == "image":
        img = add_image_watermark(img, wm_image_path, scale, opacity, rotation, position)

    return img
