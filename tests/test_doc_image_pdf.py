"""图片转 PDF 质量回归：PNG 必须无损嵌入，不能降级成 JPEG。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_png_to_pdf_preserves_pixels_and_uses_image_dpi(tmp_path):
    import pymupdf
    from PIL import Image, ImageChops
    from core.doc_converter import DocumentConverter

    source = tmp_path / "中文表格.png"
    output = tmp_path / "中文表格.pdf"
    image = Image.new("RGB", (320, 180), "white")
    # 高频锐利边缘能稳定暴露 JPEG 二次压缩造成的文字/表格模糊。
    for x in range(0, 320, 8):
        for y in range(0, 180, 8):
            if (x // 8 + y // 8) % 2:
                image.paste((15, 35, 90), (x, y, x + 8, y + 8))
    image.save(source, dpi=(96, 96))

    assert DocumentConverter().convert(str(source), str(output)) is True
    document = pymupdf.open(output)
    try:
        page = document[0]
        assert abs(page.rect.width - 240) < 0.1
        assert abs(page.rect.height - 135) < 0.1
        assert page.get_images(full=True)[0][8] == "FlateDecode"
        pixmap = page.get_pixmap(dpi=96, alpha=False)
        rendered = Image.frombytes(
            "RGB", (pixmap.width, pixmap.height), pixmap.samples)
        assert ImageChops.difference(image, rendered).getbbox() is None
    finally:
        document.close()


def test_png_without_dpi_defaults_to_96_dpi(tmp_path):
    import pymupdf
    from PIL import Image
    from core.doc_converter import DocumentConverter

    source = tmp_path / "截图.png"
    output = tmp_path / "截图.pdf"
    Image.new("RGB", (400, 200), "white").save(source)

    assert DocumentConverter().convert(str(source), str(output)) is True
    document = pymupdf.open(output)
    try:
        assert abs(document[0].rect.width - 300) < 0.1
        assert abs(document[0].rect.height - 150) < 0.1
    finally:
        document.close()
