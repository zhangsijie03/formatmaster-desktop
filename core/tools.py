"""实用工具：PDF合并拆分、图片压缩、批量重命名"""
import os
import re
import tempfile
import uuid
from PIL import Image, ImageOps


def _check_pymupdf_available():
    """检查 PyMuPDF (fitz) 是否可用
    风险规避：PyInstaller 打包后可能无法正常加载，需提前检测。
    返回 (bool, str)：(是否可用, 错误信息)
    """
    try:
        import pymupdf
        # 触发实际加载，避免仅是模块存在但运行时失败
        _ = pymupdf.__doc__
        return True, ""
    except ImportError as e:
        return False, f"PyMuPDF 未安装或打包后丢失：{e}"
    except Exception as e:
        return False, f"PyMuPDF 加载失败：{e}"


def pdf_encrypt(input_path, output_path, open_password=None, owner_password=None,
                encryption_method="AES-256", progress_cb=None):
    """PDF加密：设置打开密码和权限密码
    encryption_method: AES-128, AES-256

    风险规避：PyMuPDF 在 PyInstaller --onefile 模式下可能加载失败，
    所有 fitz 调用均包裹在 try/except 中，失败时抛出明确异常。
    """
    ok, err = _check_pymupdf_available()
    if not ok:
        raise RuntimeError(f"PDF 加密不可用：{err}")
    import pymupdf
    doc = pymupdf.open(input_path)
    
    if doc.needs_pass:
        if open_password:
            if not doc.authenticate(open_password):
                doc.close()
                raise RuntimeError("密码错误，无法打开加密文档")
        else:
            doc.close()
            raise RuntimeError("文件已加密，请输入打开密码以重新加密")
    
    if progress_cb:
        progress_cb(30, "正在加密…")
    
    perm = int(pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT | 
               pymupdf.PDF_PERM_COPY | pymupdf.PDF_PERM_ANNOTATE)
    
    encrypt_meth = pymupdf.PDF_ENCRYPT_AES_256
    if encryption_method == "AES-128":
        encrypt_meth = pymupdf.PDF_ENCRYPT_AES_128
    
    doc.save(output_path, encryption=encrypt_meth, user_pw=open_password, 
             owner_pw=owner_password, permissions=perm)
    doc.close()
    
    if progress_cb:
        progress_cb(100, "加密完成")
    return True


def pdf_decrypt(input_path, output_path, password, progress_cb=None):
    """PDF解密：移除密码保护"""
    ok, err = _check_pymupdf_available()
    if not ok:
        raise RuntimeError(f"PDF 解密不可用：{err}")
    import pymupdf
    doc = pymupdf.open(input_path)
    
    if doc.needs_pass:
        if not doc.authenticate(password):
            raise ValueError("密码错误")
    
    if progress_cb:
        progress_cb(30, "正在解密…")
    
    doc.save(output_path, encryption=pymupdf.PDF_ENCRYPT_NONE)
    doc.close()
    
    if progress_cb:
        progress_cb(100, "解密完成")
    return True


def pdf_is_encrypted(input_path):
    """检查PDF是否加密"""
    ok, _ = _check_pymupdf_available()
    if not ok:
        return False
    import pymupdf
    try:
        doc = pymupdf.open(input_path)
        result = doc.needs_pass
        doc.close()
        return result
    except Exception:
        return False


def pdf_compress(input_path, output_path, target_dpi=150, quality=80, progress_cb=None):
    """PDF压缩：降低图片分辨率，减小体积"""
    ok, err = _check_pymupdf_available()
    if not ok:
        raise RuntimeError(f"PDF 压缩不可用：{err}")
    import pymupdf
    from PIL import Image
    import io

    doc = pymupdf.open(input_path)
    total_pages = len(doc)
    
    for i in range(total_pages):
        if progress_cb:
            progress_cb(int(i * 80 / max(total_pages, 1)), f"处理第 {i+1}/{total_pages} 页…")
        
        page = doc[i]
        images = page.get_images(full=True)
        
        for img_index, img in enumerate(images):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]
            
            try:
                img_pil = Image.open(io.BytesIO(image_bytes))
                
                current_w, current_h = img_pil.size
                current_dpi = img_pil.info.get("dpi", (target_dpi, target_dpi))
                
                if current_dpi[0] > target_dpi:
                    scale = target_dpi / current_dpi[0]
                    new_w = int(current_w * scale)
                    new_h = int(current_h * scale)
                    img_pil = img_pil.resize((new_w, new_h), Image.LANCZOS)
                
                if image_ext.lower() in ('jpg', 'jpeg'):
                    img_bytes_new = io.BytesIO()
                    img_pil.save(img_bytes_new, format='JPEG', quality=quality, optimize=True)
                    img_bytes_new = img_bytes_new.getvalue()
                elif image_ext.lower() == 'png':
                    img_bytes_new = io.BytesIO()
                    img_pil.save(img_bytes_new, format='PNG', optimize=True)
                    img_bytes_new = img_bytes_new.getvalue()
                else:
                    img_bytes_new = image_bytes
                
                if len(img_bytes_new) < len(image_bytes):
                    doc.update_image(xref, img_bytes_new)
                
                img_pil.close()
            except Exception:
                pass
    
    if progress_cb:
        progress_cb(90, "正在保存…")
    
    doc.save(output_path)
    doc.close()
    
    orig_size = os.path.getsize(input_path)
    new_size = os.path.getsize(output_path)
    saved_ratio = f"{(1 - new_size / max(orig_size, 1)) * 100:.0f}%" if orig_size > 0 else "0%"
    
    if progress_cb:
        progress_cb(100, f"压缩完成  节省 {saved_ratio}")
    return True


# ═══════════════════════════════════════════════
#  PDF 合并 / 拆分
# ═══════════════════════════════════════════════
def pdf_merge(pdf_list, output_path, progress_cb=None):
    """合并多个PDF为一个"""
    from pypdf import PdfWriter
    from pypdf.errors import PdfReadError
    writer = PdfWriter()
    total = len(pdf_list)
    for i, pdf in enumerate(pdf_list):
        if progress_cb:
            progress_cb(int(i * 90 / max(total, 1)), f"合并第 {i+1}/{total} 个…")
        try:
            writer.append(pdf)
        except PdfReadError:
            if progress_cb:
                progress_cb(-1, f"错误：文件 {os.path.basename(pdf)} 已损坏或加密，无法合并")
            writer.close()
            return False
        except FileNotFoundError:
            if progress_cb:
                progress_cb(-1, f"错误：找不到文件 {os.path.basename(pdf)}")
            writer.close()
            return False
    if progress_cb:
        progress_cb(95, "正在保存…")
    try:
        writer.write(output_path)
    except Exception:
        if progress_cb:
            progress_cb(-1, "错误：无法写入输出文件，请检查磁盘空间或权限")
        writer.close()
        return False
    writer.close()
    if progress_cb:
        progress_cb(100, "合并完成")
    return True


def pdf_split(pdf_path, output_dir, page_ranges, progress_cb=None):
    """拆分PDF，page_ranges: list of (start, end) 1-based页码"""
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PdfReadError
    os.makedirs(output_dir, exist_ok=True)
    try:
        reader = PdfReader(pdf_path)
    except FileNotFoundError:
        if progress_cb:
            progress_cb(-1, "错误：找不到PDF文件")
        return False
    except PdfReadError:
        if progress_cb:
            progress_cb(-1, "错误：PDF文件已损坏或加密，无法读取")
        return False
    total_pages = len(reader.pages)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    total = len(page_ranges)

    for idx, (start, end) in enumerate(page_ranges):
        if progress_cb:
            progress_cb(int(idx * 90 / max(total, 1)),
                        f"拆分第 {start}-{end} 页…")
        if start > total_pages:
            if progress_cb:
                progress_cb(-1, f"错误：起始页码 {start} 超过总页数 {total_pages}")
            return False
        writer = PdfWriter()
        try:
            for p in range(max(start - 1, 0), min(end, total_pages)):
                writer.add_page(reader.pages[p])
        except IndexError:
            if progress_cb:
                progress_cb(-1, f"错误：页码范围 {start}-{end} 超出文档页数")
            writer.close()
            return False
        suffix = f"_p{start}-{end}" if total > 1 else ""
        out = os.path.join(output_dir, base + suffix + ".pdf")
        try:
            writer.write(out)
        except Exception:
            if progress_cb:
                progress_cb(-1, "错误：无法写入拆分文件，请检查磁盘空间或权限")
            writer.close()
            return False
        writer.close()

    if progress_cb:
        progress_cb(100, "拆分完成")
    return True


def pdf_get_page_count(pdf_path):
    from pypdf import PdfReader
    try:
        return len(PdfReader(pdf_path).pages)
    except Exception:
        return 0


# ═══════════════════════════════════════════════
#  图片压缩
# ═══════════════════════════════════════════════
_COMPRESS_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
    ".webp": "WEBP", ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
}


def _prepare_compress_image(input_path, output_ext):
    """读取并按目标格式规范色彩模式，同时保留支持透明度格式的 alpha。"""
    with Image.open(input_path) as source:
        info = dict(source.info)
        image = ImageOps.exif_transpose(source).copy()

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in info)
    if output_ext in (".jpg", ".jpeg", ".bmp"):
        if has_alpha:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image.close()
            rgba.close()
            image = background
        elif image.mode not in ("RGB", "L"):
            converted = image.convert("RGB")
            image.close()
            image = converted
    elif output_ext in (".png", ".webp", ".tif", ".tiff"):
        if has_alpha and image.mode not in ("RGBA", "LA"):
            converted = image.convert("RGBA")
            image.close()
            image = converted
        elif not has_alpha and image.mode not in ("RGB", "L"):
            converted = image.convert("RGB")
            image.close()
            image = converted
    return image, info


def _compress_save_kwargs(output_ext, quality, info):
    """生成与实际容器匹配的 Pillow 保存参数。"""
    if output_ext in (".jpg", ".jpeg"):
        kwargs = {"quality": quality, "optimize": True}
    elif output_ext == ".png":
        kwargs = {"optimize": True}
    elif output_ext == ".webp":
        kwargs = {"quality": quality, "method": 6}
    elif output_ext in (".tif", ".tiff"):
        kwargs = {"compression": "tiff_lzw"}
    else:
        kwargs = {}
    if info.get("icc_profile") and output_ext not in (".bmp",):
        kwargs["icc_profile"] = info["icc_profile"]
    return kwargs


def _create_image_stage(output_path):
    """在目标目录创建同扩展名暂存路径，供成功后原子替换。"""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".fm_image_compress_", suffix=os.path.splitext(output_path)[1],
        dir=output_dir)
    os.close(fd)
    os.remove(staged_path)
    return staged_path


def image_compress(input_path, output_path, quality=80, max_size=None, progress_cb=None):
    """压缩图片：quality 1-100，max_size (w,h) 限制最大分辨率"""
    if not os.path.isfile(input_path):
        if progress_cb:
            progress_cb(-1, "错误：找不到图片文件")
        return False
    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(
            os.path.abspath(output_path)):
        if progress_cb:
            progress_cb(-1, "错误：输出文件不能覆盖源文件")
        return False
    ext = os.path.splitext(output_path)[1].lower()
    image_format = _COMPRESS_FORMATS.get(ext)
    if image_format is None:
        if progress_cb:
            progress_cb(-1, "错误：不支持该输出图片格式")
        return False
    try:
        quality = max(1, min(100, int(quality)))
    except (TypeError, ValueError, OverflowError):
        quality = 80
    if max_size is not None:
        try:
            max_size = tuple(int(value) for value in max_size)
            if len(max_size) != 2 or min(max_size) <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            if progress_cb:
                progress_cb(-1, "错误：最大分辨率参数无效")
            return False

    if progress_cb:
        progress_cb(20, "打开图片…")
    try:
        img, source_info = _prepare_compress_image(input_path, ext)
    except (OSError, ValueError):
        if progress_cb:
            progress_cb(-1, "错误：无法打开图片，文件可能已损坏")
        return False

    try:
        if progress_cb:
            progress_cb(50, "压缩中…")
    except InterruptedError:
        img.close()
        raise

    # 限制最大分辨率
    if max_size:
        try:
            w, h = img.size
            mw, mh = max_size
            if w > mw or h > mh:
                ratio = min(mw / w, mh / h)
                resized = img.resize(
                    (max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
                img.close()
                img = resized
        except (OSError, TypeError, ValueError):
            if progress_cb:
                progress_cb(-1, "错误：图片缩放失败")
            img.close()
            return False

    save_kw = _compress_save_kwargs(ext, quality, source_info)

    try:
        if progress_cb:
            progress_cb(80, "保存…")
    except InterruptedError:
        img.close()
        raise
    staged_path = ""
    try:
        staged_path = _create_image_stage(output_path)
        img.save(staged_path, format=image_format, **save_kw)
        if not os.path.isfile(staged_path) or os.path.getsize(staged_path) <= 0:
            raise OSError("输出文件未生成")
        os.replace(staged_path, output_path)
    except (OSError, ValueError):
        if progress_cb:
            progress_cb(-1, "错误：无法保存压缩图片，请检查磁盘空间或权限")
        return False
    finally:
        img.close()
        try:
            if staged_path and os.path.exists(staged_path):
                os.remove(staged_path)
        except OSError:
            pass

    orig = os.path.getsize(input_path)
    new = os.path.getsize(output_path)
    change = abs(new - orig) / max(orig, 1) * 100 if orig > 0 else 0
    if progress_cb:
        message = (f"压缩完成  节省 {change:.0f}%" if new <= orig
                   else f"处理完成  体积增加 {change:.0f}%")
        progress_cb(100, message)
    return True


def image_compress_to_size(input_path, output_path, target_kb,
                           progress_cb=None, min_quality=25):
    """把图片压到「不超过 target_kb」，自动调质量/分辨率（二分迭代）。

    - 先降 JPEG/WebP quality（85 → 每次 -10），仍超再等比缩小分辨率
      （0.85x 阶梯），直到 ≤ target_kb；确实无法达到时返回失败。
    - PNG/BMP/TIFF 无有损 quality，直接走分辨率阶梯。
    返回 (ok, msg, final_size)；final_size 为最终字节数。
    """
    if not os.path.isfile(input_path):
        if progress_cb:
            progress_cb(-1, "错误：找不到图片文件")
        return False, "找不到图片文件", 0
    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(
            os.path.abspath(output_path)):
        if progress_cb:
            progress_cb(-1, "错误：输出文件不能覆盖源文件")
        return False, "输出文件不能覆盖源文件", 0
    ext = os.path.splitext(output_path)[1].lower()
    image_format = _COMPRESS_FORMATS.get(ext)
    if image_format is None:
        if progress_cb:
            progress_cb(-1, "错误：不支持该输出图片格式")
        return False, "不支持该输出图片格式", 0
    try:
        target = int(target_kb) * 1024
        min_quality = max(1, min(95, int(min_quality)))
        if target <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        if progress_cb:
            progress_cb(-1, "错误：目标大小参数无效")
        return False, "目标大小参数无效", 0

    try:
        img, source_info = _prepare_compress_image(input_path, ext)
    except (OSError, ValueError):
        if progress_cb:
            progress_cb(-1, "错误：无法打开图片")
        return False, "无法打开图片", 0

    import io

    def _size_of(im, q):
        buf = io.BytesIO()
        kw = _compress_save_kwargs(ext, q, source_info)
        im.save(buf, format=image_format, **kw)
        return buf.getvalue()

    if progress_cb:
        progress_cb(30, "迭代压缩…")

    quality = 85
    scale = 1.0
    best_data = None
    try:
        for iteration in range(40):
            if progress_cb:
                progress_cb(30 + int(iteration * 60 / 40), "迭代压缩…")
            w = max(2, int(img.width * scale))
            h = max(2, int(img.height * scale))
            im = None
            try:
                im = img.resize((w, h), Image.LANCZOS) if scale < 1.0 else img
                data = _size_of(im, quality)
            except (OSError, ValueError):
                break
            finally:
                if im is not None and im is not img:
                    im.close()
            if len(data) <= target:
                best_data = data
                break
            # 记录当前最接近目标的
            if best_data is None or len(data) < len(best_data):
                best_data = data
            if ext in (".jpg", ".jpeg", ".webp") and quality > min_quality:
                quality -= 10          # 先降质量
                quality = max(min_quality, quality)
            else:
                scale *= 0.85          # 质量到底再降分辨率
                if w <= 2 and h <= 2:
                    break
    finally:
        img.close()

    if best_data is None or len(best_data) > target:
        actual_kb = len(best_data) // 1024 if best_data else 0
        msg = f"无法压至 {target_kb}KB（当前最小约 {actual_kb}KB）"
        if progress_cb:
            progress_cb(-1, f"错误：{msg}")
        return False, msg, len(best_data) if best_data else 0

    staged_path = ""
    try:
        staged_path = _create_image_stage(output_path)
        with open(staged_path, "wb") as f:
            f.write(best_data)
        os.replace(staged_path, output_path)
    except OSError:
        if progress_cb:
            progress_cb(-1, "错误：无法写入输出文件")
        return False, "写入失败", 0
    finally:
        try:
            if staged_path and os.path.exists(staged_path):
                os.remove(staged_path)
        except OSError:
            pass
    if progress_cb:
        progress_cb(100, "压缩完成")
    return True, "压缩完成", len(best_data)


# ═══════════════════════════════════════════════
#  PDF 批量水印 & 页码
# ═══════════════════════════════════════════════

def pdf_add_watermark(input_path, output_path, text, pos="右下角",
                      opacity=0.3, rotation=0, progress_cb=None):
    import pymupdf
    if progress_cb: progress_cb(10, "打开PDF...")
    doc = pymupdf.open(input_path)
    positions = {
        "左上角": (0.05, 0.05), "右上角": (0.65, 0.05),
        "左下角": (0.05, 0.85), "右下角": (0.65, 0.85),
        "居中":   (0.35, 0.45),
    }
    rx, ry = positions.get(pos, (0.65, 0.85))
    total = len(doc)
    for i in range(total):
        if progress_cb: progress_cb(20 + int(70 * i / total), f"添加水印 {i+1}/{total}")
        page = doc[i]
        r = page.rect
        x = r.x0 + r.width * rx
        y = r.y0 + r.height * ry
        annot = page.add_freetext_annot(
            pymupdf.Rect(x, y, x + r.width * 0.3, y + r.height * 0.1),
            text, fontsize=max(12, r.width / 50), fontname="helv",
            text_color=0.5, fill_color=None, border_width=0,
        )
        annot.set_opacity(opacity)
        if rotation:
            annot.set_rotation(rotation)
        annot.update()
    if progress_cb: progress_cb(95, "保存...")
    doc.save(output_path, deflate=True, garbage=4)
    doc.close()
    if progress_cb: progress_cb(100, "完成")
    return True


def pdf_add_page_numbers(input_path, output_path, start=1, pos="底部居中",
                         fmt="{n}", progress_cb=None):
    import pymupdf
    if progress_cb: progress_cb(10, "打开PDF...")
    doc = pymupdf.open(input_path)
    positions = {
        "底部居中": (0.5, 0.95), "底部左对齐": (0.05, 0.95),
        "底部右对齐": (0.85, 0.95), "顶部居中": (0.5, 0.03),
    }
    rx, ry = positions.get(pos, (0.5, 0.95))
    total = len(doc)
    for i in range(total):
        if progress_cb: progress_cb(20 + int(70 * i / total), f"添加页码 {i+1}/{total}")
        page = doc[i]
        r = page.rect
        num = start + i
        text = fmt.replace("{n}", str(num))
        page.insert_text(
            pymupdf.Point(r.x0 + r.width * rx, r.y0 + r.height * ry),
            text, fontname="helv", fontsize=10, color=(0.4, 0.4, 0.4),
        )
    if progress_cb: progress_cb(95, "保存...")
    doc.save(output_path, deflate=True, garbage=4)
    doc.close()
    if progress_cb: progress_cb(100, "完成")
    return True


# ═══════════════════════════════════════════════
#  批量重命名
# ═══════════════════════════════════════════════
_RENAME_CASES = {"none", "upper", "lower", "title"}


def _rename_path_key(path):
    """生成冲突检查键，提前覆盖 macOS 大小写不敏感语义。"""
    return os.path.normcase(os.path.abspath(path)).casefold()


def _validate_rename_name(name):
    """确保模板只生成文件名，不会变成路径或目录穿越。"""
    if not name or name in (".", ".."):
        raise ValueError("新文件名不能为空或目录名")
    separators = {os.sep, "/", "\\"}
    if os.altsep:
        separators.add(os.altsep)
    if any(sep and sep in name for sep in separators):
        raise ValueError("命名模板不能包含路径分隔符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("新文件名不能包含控制字符")


def _validate_rename_plan(plan):
    """在改动磁盘前一次性发现缺失源、重复目标与外部冲突。"""
    source_keys = set()
    target_keys = set()
    source_paths = []
    for src, new_name, target in plan:
        _validate_rename_name(new_name)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"找不到源文件：{src}")
        source_key = _rename_path_key(src)
        if source_key in source_keys:
            raise ValueError(f"文件列表包含重复项：{src}")
        source_keys.add(source_key)
        source_paths.append((src, target))

        target_key = _rename_path_key(target)
        if target_key in target_keys:
            raise FileExistsError(f"多个文件将重命名为同一目标：{new_name}")
        target_keys.add(target_key)
        parent = os.path.dirname(os.path.abspath(target))
        if not os.path.isdir(parent):
            raise FileNotFoundError(f"目标文件夹不存在：{parent}")

    for src, target in source_paths:
        same_path = os.path.abspath(src) == os.path.abspath(target)
        if (not same_path and os.path.exists(target)
                and _rename_path_key(target) not in source_keys):
            raise FileExistsError(f"目标文件已存在：{target}")


def build_rename_plan(file_list, pattern, start_num=1, output_dir=None,
                      search_text="", replace_text="", case="none",
                      regex_pattern="", regex_replace=""):
    """计算并完整校验重命名方案（dry-run，不改动磁盘）。"""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("命名模板不能为空")
    if not isinstance(start_num, int) or isinstance(start_num, bool) or start_num < 0:
        raise ValueError("开始序号必须是大于或等于 0 的整数")
    if case not in _RENAME_CASES:
        raise ValueError(f"不支持的大小写规则：{case}")
    try:
        regex = re.compile(regex_pattern) if regex_pattern else None
    except re.error as exc:
        raise ValueError(f"正则表达式无效：{exc}") from exc
    plan = []
    for i, fp in enumerate(file_list):
        directory = output_dir if output_dir else os.path.dirname(fp)
        name = os.path.splitext(os.path.basename(fp))[0]
        ext = os.path.splitext(fp)[1]
        folder_str = os.path.basename(os.path.dirname(fp))
        try:
            mtime = os.path.getmtime(fp)
            dt = __import__("datetime").datetime.fromtimestamp(mtime)
            date_str = dt.strftime("%Y%m%d")
            time_str = dt.strftime("%H%M%S")
        except OSError:
            date_str = "00000000"
            time_str = "000000"

        number = start_num + i
        rendered = pattern.replace("{name}", name).replace("{ext}", ext)
        rendered = rendered.replace("{date}", date_str)
        rendered = rendered.replace("{time}", time_str).replace(
            "{folder}", folder_str)

        def _format_number(match):
            spec = match.group(1).lstrip(":") if match.group(1) else ""
            return format(number, spec) if spec else str(number)

        try:
            rendered = re.sub(r"\{n(:.*?)?\}", _format_number, rendered)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"序号格式无效：{exc}") from exc
        if "{" in rendered or "}" in rendered:
            raise ValueError("命名模板包含未识别的占位符")

        new_name = rendered + ext if not rendered.endswith(ext) else rendered
        if search_text:
            new_name = new_name.replace(search_text, replace_text)
        if regex is not None:
            new_name = regex.sub(regex_replace, new_name)
        if case in ("upper", "lower", "title"):
            body, new_ext = os.path.splitext(new_name)
            if case == "upper":
                body = body.upper()
            elif case == "lower":
                body = body.lower()
            else:
                body = body.title()
            new_name = body + new_ext

        _validate_rename_name(new_name)
        plan.append((fp, new_name, os.path.join(directory, new_name)))
    _validate_rename_plan(plan)
    return plan


def execute_rename_plan(plan, progress_cb=None):
    """两阶段执行已预览计划，支持文件名互换并在失败时回滚。"""
    plan = list(plan)
    _validate_rename_plan(plan)
    changed = [item for item in plan
               if os.path.abspath(item[0]) != os.path.abspath(item[2])]
    if not changed:
        if progress_cb:
            progress_cb(100, "没有需要重命名的文件")
        return []

    staged = []
    completed = []
    total = len(changed)
    try:
        for index, (src, _new_name, target) in enumerate(changed):
            temp_name = (f".{os.path.basename(src)}.formatmaster-rename-"
                         f"{uuid.uuid4().hex}")
            temp_path = os.path.join(os.path.dirname(src), temp_name)
            os.rename(src, temp_path)
            staged.append((src, temp_path, target))
            if progress_cb:
                progress_cb(int((index + 1) * 45 / total),
                            f"准备重命名 {index + 1}/{total}…")

        for index, (src, temp_path, target) in enumerate(staged):
            os.rename(temp_path, target)
            completed.append((src, temp_path, target))
            if progress_cb:
                progress_cb(45 + int((index + 1) * 55 / total),
                            f"重命名 {index + 1}/{total}…")
    except OSError as exc:
        # 先将已落到目标名的文件收回临时名，再统一恢复原名。
        for _src, temp_path, target in reversed(completed):
            if os.path.exists(target) and not os.path.exists(temp_path):
                try:
                    os.rename(target, temp_path)
                except OSError:
                    pass
        rollback_errors = []
        for src, temp_path, _target in reversed(staged):
            if os.path.exists(temp_path):
                try:
                    os.rename(temp_path, src)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            message = f"重命名失败，且有文件未能自动恢复：{exc}"
        else:
            message = f"重命名失败，已恢复原文件：{exc}"
        raise OSError(message) from exc

    return [(src, target) for src, _temp_path, target in staged]


def batch_rename(file_list, pattern, start_num=1, progress_cb=None, output_dir=None,
                 search_text="", replace_text="", case="none",
                 regex_pattern="", regex_replace=""):
    """校验并执行批量重命名；预览页可直接执行已生成计划。"""
    plan = build_rename_plan(
        file_list, pattern, start_num=start_num, output_dir=output_dir,
        search_text=search_text, replace_text=replace_text, case=case,
        regex_pattern=regex_pattern, regex_replace=regex_replace)
    renamed = execute_rename_plan(plan, progress_cb=progress_cb)
    if progress_cb:
        progress_cb(100, f"重命名完成  {len(renamed)} 个文件")
    return renamed
