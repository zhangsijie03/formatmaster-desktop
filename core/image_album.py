"""image_album — 图片拼接 / 合成 PDF 相册。

- merge_vertical / merge_horizontal：多图拼成一张长图（宽度/高度统一，白底空隙）
- to_pdf：多图一键合成 PDF 相册（每图一页，A4 或按原尺寸）
纯 PIL 实现，无外部依赖。
"""
import os
import tempfile

from PIL import Image, ImageOps

_BG = (255, 255, 255)
_IMAGE_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
    ".webp": "WEBP", ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
}
_MAX_CANVAS_PIXELS = 120_000_000
_JPEG_MAX_DIMENSION = 65_500


def _load(files, progress_callback=None):
    """打开全部图片、应用 EXIF 方向，并以白底安全合成透明区域。"""
    if not isinstance(files, (list, tuple)) or not files:
        if progress_callback:
            progress_callback(-1, "请至少提供一张图片")
        return None
    imgs = []
    total = len(files)
    try:
        for i, f in enumerate(files):
            with Image.open(f) as source:
                oriented = ImageOps.exif_transpose(source)
                has_alpha = oriented.mode in ("RGBA", "LA") or (
                    oriented.mode == "P" and "transparency" in source.info)
                if has_alpha:
                    rgba = oriented.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    im = Image.new("RGB", rgba.size, _BG)
                    im.paste(rgba, mask=alpha)
                    alpha.close()
                    rgba.close()
                else:
                    im = oriented.convert("RGB")
                if oriented is not source:
                    oriented.close()
            imgs.append(im)
            if progress_callback:
                progress_callback(
                    int((i + 1) / total * 40), f"读取 {i+1}/{total}…")
        return imgs
    except InterruptedError:
        _close(imgs)
        raise
    except (OSError, IOError, ValueError) as exc:
        _close(imgs)
        if progress_callback:
            progress_callback(
                -1, f"无法打开 {os.path.basename(str(f))}：{exc}")
        return None


def _close(imgs):
    for im in imgs:
        try:
            im.close()
        except Exception:
            pass


def _validate_request(files, output_path, gap, progress_callback):
    """校验公共输入，防止覆盖源图或用异常间距制造巨型画布。"""
    if not isinstance(files, (list, tuple)) or not files:
        if progress_callback:
            progress_callback(-1, "请至少提供一张图片")
        return None
    output_abs = os.path.normcase(os.path.abspath(output_path))
    if any(output_abs == os.path.normcase(os.path.abspath(path))
           for path in files):
        if progress_callback:
            progress_callback(-1, "输出文件不能覆盖任一源图片")
        return None
    try:
        gap = int(gap)
    except (TypeError, ValueError, OverflowError):
        gap = -1
    if gap < 0 or gap > 10_000:
        if progress_callback:
            progress_callback(-1, "图片间距必须在 0 到 10000 像素之间")
        return None
    return gap


def _validate_canvas(width, height, output_path, progress_callback):
    ext = os.path.splitext(output_path)[1].lower()
    if ext not in _IMAGE_FORMATS:
        if progress_callback:
            progress_callback(-1, "不支持该输出图片格式")
        return False
    if width <= 0 or height <= 0 or width * height > _MAX_CANVAS_PIXELS:
        if progress_callback:
            progress_callback(-1, "拼接结果尺寸过大，请减少图片数量或分辨率")
        return False
    if ext in (".jpg", ".jpeg") and max(width, height) > _JPEG_MAX_DIMENSION:
        if progress_callback:
            progress_callback(-1, "拼接结果超过 JPEG 最大边长，请减少图片数量")
        return False
    return True


def _atomic_write(output_path, suffix, writer, progress_callback):
    """同目录写临时文件，成功且未取消后再替换正式结果。"""
    staged_path = ""
    try:
        output_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(output_dir, exist_ok=True)
        fd, staged_path = tempfile.mkstemp(
            prefix=".fm_image_merge_", suffix=suffix, dir=output_dir)
        os.close(fd)
        os.remove(staged_path)
        writer(staged_path)
        if not os.path.isfile(staged_path) or os.path.getsize(staged_path) <= 0:
            raise OSError("输出文件未生成")
        if progress_callback:
            progress_callback(95, "写入结果…")
        os.replace(staged_path, output_path)
        return True
    except InterruptedError:
        raise
    except (OSError, IOError, ValueError, MemoryError) as exc:
        if progress_callback:
            progress_callback(-1, f"保存失败：{exc}")
        return False
    finally:
        try:
            if staged_path and os.path.exists(staged_path):
                os.remove(staged_path)
        except OSError:
            pass


def _merge(files, output_path, gap, vertical, progress_callback):
    gap = _validate_request(files, output_path, gap, progress_callback)
    if gap is None:
        return False
    imgs = _load(files, progress_callback)
    if imgs is None:
        return False
    canvas = None
    try:
        if vertical:
            target = max(image.width for image in imgs)
            sizes = [(target, max(1, round(image.height * target / image.width)))
                     for image in imgs]
            width = target
            height = sum(size[1] for size in sizes) + gap * (len(imgs) - 1)
        else:
            target = max(image.height for image in imgs)
            sizes = [(max(1, round(image.width * target / image.height)), target)
                     for image in imgs]
            width = sum(size[0] for size in sizes) + gap * (len(imgs) - 1)
            height = target
        if not _validate_canvas(width, height, output_path, progress_callback):
            return False
        canvas = Image.new("RGB", (width, height), _BG)
        cursor = 0
        for index, (image, size) in enumerate(zip(imgs, sizes)):
            placed = image if image.size == size else image.resize(size, Image.LANCZOS)
            canvas.paste(placed, (0, cursor) if vertical else (cursor, 0))
            cursor += (size[1] if vertical else size[0]) + gap
            if placed is not image:
                placed.close()
            if progress_callback:
                progress_callback(
                    int(40 + (index + 1) / len(imgs) * 50),
                    f"拼接 {index + 1}/{len(imgs)}…")
        ext = os.path.splitext(output_path)[1].lower()
        image_format = _IMAGE_FORMATS[ext]
        save_kwargs = {"quality": 95} if image_format in ("JPEG", "WEBP") else {}
        return _atomic_write(
            output_path, ext,
            lambda staged: canvas.save(
                staged, format=image_format, **save_kwargs),
            progress_callback)
    except InterruptedError:
        raise
    except (OSError, ValueError, MemoryError) as exc:
        if progress_callback:
            progress_callback(-1, f"拼接失败：{exc}")
        return False
    finally:
        if canvas is not None:
            canvas.close()
        _close(imgs)


def merge_vertical(files, output_path, gap=10, progress_callback=None):
    """纵向拼接：所有图统一到最大宽度，依次向下排列。"""
    return _merge(files, output_path, gap, True, progress_callback)


def merge_horizontal(files, output_path, gap=10, progress_callback=None):
    """横向拼接：所有图统一到最大高度，依次向右排列。"""
    return _merge(files, output_path, gap, False, progress_callback)


def to_pdf(files, output_path, page_mode="A4", progress_callback=None):
    """多图合成 PDF 相册：每张图一页。

    page_mode: "A4"（A4 纸居中）或 "original"（按图片原始尺寸）。
    """
    if _validate_request(files, output_path, 0, progress_callback) is None:
        return False
    if page_mode not in ("A4", "original"):
        if progress_callback:
            progress_callback(-1, "不支持该 PDF 页面模式")
        return False
    if os.path.splitext(output_path)[1].lower() != ".pdf":
        if progress_callback:
            progress_callback(-1, "PDF 相册必须输出为 .pdf 文件")
        return False
    imgs = _load(files, progress_callback)
    if imgs is None:
        return False
    pages = []
    try:
        if page_mode == "original":
            if progress_callback:
                progress_callback(90, "准备原尺寸 PDF…")
            writer = lambda staged: imgs[0].save(
                staged, "PDF", save_all=True,
                append_images=imgs[1:] if len(imgs) > 1 else [])
        else:
            # A4（2480×3508 @300dpi），白底居中，等比缩放
            page = (2480, 3508)
            for index, im in enumerate(imgs):
                canvas = Image.new("RGB", page, _BG)
                scale = min(page[0] / im.width, page[1] / im.height, 1.0)
                placed = im
                if scale < 1.0:
                    placed = im.resize(
                        (max(1, round(im.width * scale)),
                         max(1, round(im.height * scale))), Image.LANCZOS)
                canvas.paste(
                    placed, ((page[0] - placed.width) // 2,
                             (page[1] - placed.height) // 2))
                if placed is not im:
                    placed.close()
                pages.append(canvas)
                if progress_callback:
                    progress_callback(
                        int(40 + (index + 1) / len(imgs) * 50),
                        f"排版 {index + 1}/{len(imgs)}…")
            writer = lambda staged: pages[0].save(
                staged, "PDF", save_all=True,
                append_images=pages[1:] if len(pages) > 1 else [])
        return _atomic_write(
            output_path, ".pdf", writer, progress_callback)
    except InterruptedError:
        raise
    except (OSError, IOError, ValueError, MemoryError) as e:
        if progress_callback:
            progress_callback(-1, f"保存失败：{e}")
        return False
    finally:
        _close(pages)
        _close(imgs)
