# -*- coding: utf-8 -*-
"""qr_maker — 二维码美化生成器（2026-08-19 新增）。

在 qrcode 库基础上增强外观：
- 模块样式：经典方块 / 圆角方块 / 圆点 / 菱形
- 前景渐变：无 / 垂直 / 对角（fg → fg 亮化色）
- Logo 嵌入：中心圆形遮罩 + 白边（需高容错 ERROR_CORRECT_H）
- 颜色/边距/尺寸自定义

全部为纯内存 Pillow 操作，毫秒级，可同步调用。
"""
import os

from PIL import Image, ImageDraw

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
except Exception:  # noqa: BLE001 - 依赖缺失降级
    qrcode = None

# 模块样式
STYLE_SQUARE = "square"          # 经典方块
STYLE_ROUNDED = "rounded"        # 圆角方块
STYLE_DOT = "dot"                # 圆点
STYLE_DIAMOND = "diamond"        # 菱形
STYLES = (STYLE_SQUARE, STYLE_ROUNDED, STYLE_DOT, STYLE_DIAMOND)

# 渐变方向
GRAD_NONE = "none"
GRAD_VERTICAL = "vertical"
GRAD_DIAGONAL = "diagonal"
GRADIENTS = (GRAD_NONE, GRAD_VERTICAL, GRAD_DIAGONAL)
MIN_BORDER = 4
MIN_MODULE_PIXELS = 2
RECOMMENDED_MODULE_PIXELS = 3


def _parse_color(text, default="#000000"):
    """支持 #RRGGBB / #RGB / 颜色名，失败回退默认。"""
    try:
        return Image.new("RGB", (1, 1), (text or default)).getpixel((0, 0))
    except Exception:  # noqa: BLE001
        try:
            return Image.new("RGB", (1, 1), default).getpixel((0, 0))
        except Exception:  # noqa: BLE001
            return (0, 0, 0)


def _lighten(rgb, factor=0.45):
    """向白色方向混合 factor，得到渐变末端色。"""
    return tuple(int(c + (255 - c) * factor) for c in rgb)


def _relative_luminance(rgb):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first, second):
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _safe_gradient_end(foreground, background):
    """限制渐变末端，避免浅色模块在背景上失去扫码所需对比度。"""
    for step in range(9, -1, -1):
        candidate = _lighten(foreground, 0.05 * step)
        if _contrast_ratio(candidate, background) >= 3.0:
            return candidate
    return foreground


def _is_finder_module(row, column, border, module_count):
    """定位图案始终使用实心方块，花式模块不能破坏三个扫描锚点。"""
    anchors = (
        (border, border),
        (border, border + module_count - 7),
        (border + module_count - 7, border),
    )
    return any(top <= row < top + 7 and left <= column < left + 7
               for top, left in anchors)


def _grad_color(c1, c2, t):
    """t∈[0,1] 线性插值两个颜色。"""
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _draw_module(draw, x, y, m, color, style):
    """按样式绘制一个模块（坐标已含边距换算）。"""
    if style == STYLE_SQUARE:
        draw.rectangle([x, y, x + m - 1, y + m - 1], fill=color)
    elif style == STYLE_ROUNDED:
        r = max(1, m // 4)
        draw.rounded_rectangle([x, y, x + m - 1, y + m - 1], radius=r,
                               fill=color)
    elif style == STYLE_DOT:
        r = m // 2 - 1
        cx, cy = x + m / 2, y + m / 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    else:  # diamond
        cx, cy = x + m / 2, y + m / 2
        r = m / 2 - 1
        draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r),
                      (cx - r, cy)], fill=color)


def make_fancy_qr(content, size=400, fg="#000000", bg="#FFFFFF",
                  style=STYLE_SQUARE, gradient=GRAD_NONE,
                  logo_path=None, border=4, logo_ratio=0.24,
                  min_module_pixels=MIN_MODULE_PIXELS):
    """生成美化二维码 PIL Image。

    content: 文本/网址/WiFi/名片内容
    size: 输出像素尺寸
    fg/bg: 前景/背景色
    style: square / rounded / dot / diamond
    gradient: none / vertical / diagonal（fg → 亮化色）
    logo_path: 中心 Logo 图片路径（可选，圆形遮罩+白边）
    border: 空白边距（模块数）
    logo_ratio: Logo 占二维码宽度的比例
    """
    if qrcode is None:
        raise RuntimeError("缺少 qrcode 库，请先安装：pip install qrcode[pil]")
    content = str(content)
    if not content:
        raise ValueError("二维码内容不能为空")
    size = int(size)
    if not 120 <= size <= 4096:
        raise ValueError("二维码尺寸必须在 120～4096 像素之间")
    border = max(MIN_BORDER, int(border) if border is not None else MIN_BORDER)
    if style not in STYLES:
        style = STYLE_SQUARE
    if gradient not in GRADIENTS:
        gradient = GRAD_NONE

    fg_c = _parse_color(fg, "#000000")
    bg_c = _parse_color(bg, "#FFFFFF")
    if _contrast_ratio(fg_c, bg_c) < 3.0:
        raise ValueError("前景色与背景色对比度过低，请改用更深/更浅的颜色")

    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H,
                       box_size=10, border=border)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()                # 包含 quiet zone 边距
    count = len(matrix)

    min_module_pixels = max(1, int(min_module_pixels))
    m = size // count
    if m < min_module_pixels:
        minimum_size = count * min_module_pixels
        raise ValueError(f"内容过长，当前尺寸不足；请选择至少 {minimum_size}×{minimum_size}")
    canvas = Image.new("RGB", (m * count, m * count), bg_c)
    draw = ImageDraw.Draw(canvas)

    grad_end = _safe_gradient_end(fg_c, bg_c)

    for i, row in enumerate(matrix):
        for j, cell in enumerate(row):
            if not cell:
                continue
            t = 0.0
            if gradient == GRAD_VERTICAL:
                t = i / max(count - 1, 1)
            elif gradient == GRAD_DIAGONAL:
                t = (i + j) / max(2 * (count - 1), 1)
            color = _grad_color(fg_c, grad_end, t) if gradient != GRAD_NONE \
                else fg_c
            module_style = STYLE_SQUARE if _is_finder_module(
                i, j, border, qr.modules_count) else style
            _draw_module(draw, j * m, i * m, m, color, module_style)

    # 二维码必须保持模块边缘清晰；LANCZOS 会在边缘制造灰色像素并降低识别率。
    canvas = canvas.resize((size, size), Image.Resampling.NEAREST)

    # 中心 Logo（圆形遮罩 + 白边，白边提升辨识度与可扫描性）
    if logo_path:
        if not os.path.isfile(logo_path):
            raise ValueError("Logo 文件不存在或已被移动")
        try:
            with Image.open(logo_path) as _f:
                logo = _f.convert("RGBA")
            logo_ratio = min(0.22, max(0.05, float(logo_ratio)))
            logo_w = int(size * logo_ratio)
            logo = logo.resize((logo_w, logo_w), Image.LANCZOS)
            pad = max(2, logo_w // 8)
            emblem = Image.new("RGBA", (logo_w + pad * 2, logo_w + pad * 2),
                               (*bg_c, 255))
            mask = Image.new("L", (logo_w + pad * 2, logo_w + pad * 2), 0)
            ImageDraw.Draw(mask).ellipse(
                [0, 0, logo_w + pad * 2 - 1, logo_w + pad * 2 - 1], fill=255)
            emblem.paste(logo, (pad, pad))
            emblem.putalpha(mask)
            ox = (size - emblem.size[0]) // 2
            oy = (size - emblem.size[1]) // 2
            canvas.paste(emblem, (ox, oy), emblem)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Logo 图片无法读取：{exc}") from exc
    return canvas
