"""图像批量预设裁剪"""
import os
from PIL import Image, ImageOps

from gui_qt.i18n import tr


PRESETS = {
    tr("1:1 正方形 (1080×1080)", "1:1 Square (1080×1080)"): (1080, 1080),
    tr("4:3 横版 (1200×900)", "4:3 Landscape (1200×900)"): (1200, 900),
    tr("16:9 横版 (1920×1080)", "16:9 Landscape (1920×1080)"): (1920, 1080),
    tr("9:16 竖版 (1080×1920)", "9:16 Portrait (1080×1920)"): (1080, 1920),
    tr("3:4 竖版 (900×1200)", "3:4 Portrait (900×1200)"): (900, 1200),
    tr("微信封面 (900×383)", "WeChat Cover (900×383)"): (900, 383),
    tr("小红书竖版 (1242×1660)", "Xiaohongshu Portrait (1242×1660)"): (1242, 1660),
    tr("抖音竖版 (720×1280)", "Douyin Portrait (720×1280)"): (720, 1280),
    tr("B站封面 (1146×717)", "Bilibili Cover (1146×717)"): (1146, 717),
    tr("微博封面 (980×300)", "Weibo Cover (980×300)"): (980, 300),
    "YouTube 封面 (1280×720)": (1280, 720),
}


def crop_to_preset(input_path, output_path, preset_size, mode="cover", progress_cb=None):
    try:
        # with + copy：完整解码后释放句柄（裁剪输出可能覆盖源文件）
        with Image.open(input_path) as _f:
            # 手机照片常依赖 EXIF Orientation，先转成视觉方向再计算比例。
            img = ImageOps.exif_transpose(_f).copy()
    except FileNotFoundError:
        if progress_cb:
            progress_cb(-1, "错误：找不到图片文件")
        return False
    except Exception:
        if progress_cb:
            progress_cb(-1, "错误：无法打开图片")
        return False

    target_w, target_h = preset_size
    if mode == "cover":
        source = img
        img = ImageOps.fit(
            source, (target_w, target_h), method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5))
        source.close()
    else:
        # fit 的语义是完整显示并留白，不能通过改变宽高比把原图拉伸。
        fitted = ImageOps.contain(
            img, (target_w, target_h), method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
        left = (target_w - fitted.width) // 2
        top = (target_h - fitted.height) // 2
        if fitted.mode in ("RGBA", "LA"):
            canvas.paste(fitted, (left, top), fitted.getchannel("A"))
        else:
            canvas.paste(fitted.convert("RGB"), (left, top))
        img.close()
        fitted.close()
        img = canvas

    if img.mode == 'RGBA':
        source = img
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(source, mask=source.getchannel('A'))
        img = bg
        source.close()
    elif img.mode not in ('RGB', 'L'):
        source = img
        img = source.convert('RGB')
        source.close()

    if progress_cb:
        progress_cb(80, "保存…")
    try:
        img.save(output_path, format="JPEG", quality=95, optimize=True)
    except OSError:
        if progress_cb:
            progress_cb(-1, "错误：无法写入输出文件")
        return False
    finally:
        img.close()
    if progress_cb:
        progress_cb(100, "裁剪完成")
    return True


def batch_crop(files, output_dir, preset_size, mode="cover", progress_cb=None):
    os.makedirs(output_dir, exist_ok=True)
    total = len(files)
    success = 0
    used_paths = set()
    for i, fp in enumerate(files):
        if progress_cb:
            progress_cb(int(i * 90 / max(total, 1)), f"处理 {i+1}/{total}…")
        stem = os.path.splitext(os.path.basename(fp))[0]
        target_w, target_h = preset_size
        base = os.path.join(output_dir, f"{stem}_{target_w}x{target_h}.jpg")
        out = base
        counter = 2
        # 输出始终带尺寸后缀，且对磁盘已有文件和本批同名文件都避让。
        while out in used_paths or os.path.exists(out):
            out = os.path.join(
                output_dir, f"{stem}_{target_w}x{target_h}_{counter}.jpg")
            counter += 1
        used_paths.add(out)

        start = int(i * 90 / max(total, 1))
        span = max(1, int(90 / max(total, 1)))

        def _item_progress(pct, message):
            if progress_cb:
                if pct < 0:
                    progress_cb(pct, message)
                else:
                    progress_cb(min(90, start + int(span * pct / 100)), message)

        if crop_to_preset(fp, out, preset_size, mode, _item_progress):
            success += 1
    if progress_cb:
        progress_cb(100, f"完成  {success}/{total}")
    return success
