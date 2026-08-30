"""“水印处理”菜单审查后的核心正确性与数据安全测试。"""

import os

from PIL import Image, ImageChops


def test_named_positions_are_geometrically_correct():
    from core.watermark_tool import _calc_position

    expected = {
        "top_left": (20, 10),
        "top_right": (780, 10),
        "bottom_left": (20, 390),
        "bottom_right": (780, 390),
        "center": (400, 200),
    }
    for key, point in expected.items():
        assert _calc_position(1000, 500, 200, 100, key) == point
    assert _calc_position(1000, 500, 200, 100, "Top left") == expected["top_left"]
    assert _calc_position(1000, 500, 200, 100, "居中") == expected["center"]


def test_text_watermark_preserves_png_transparency(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGBA", (160, 100), (0, 0, 0, 0)).save(source)
    assert process_watermark(
        str(source), str(output), wm_type="text", text="WM",
        color="#ff0000", position="top_left")
    with Image.open(output) as result:
        assert result.format == "PNG"
        assert "A" in result.getbands()
        assert result.getpixel((159, 99))[3] == 0


def test_image_watermark_changes_pixels(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "source.png"
    watermark = tmp_path / "watermark.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (200, 120), "white").save(source)
    Image.new("RGBA", (40, 20), (255, 0, 0, 180)).save(watermark)
    assert process_watermark(
        str(source), str(output), wm_type="image",
        wm_image_path=str(watermark), position="center", scale=0.2)
    with Image.open(source) as before, Image.open(output) as after:
        assert ImageChops.difference(
            before.convert("RGB"), after.convert("RGB")).getbbox()


def test_missing_or_broken_watermark_image_is_failure(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "source.png"
    broken = tmp_path / "broken.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (80, 60), "white").save(source)
    broken.write_bytes(b"not-an-image")
    messages = []
    assert not process_watermark(
        str(source), str(output), wm_type="image",
        wm_image_path=str(broken), progress_cb=lambda _pct, msg: messages.append(msg))
    assert not output.exists()
    assert messages and "处理失败" in messages[-1]


def test_failure_preserves_existing_output(monkeypatch, tmp_path):
    from core import watermark_tool

    source = tmp_path / "source.jpg"
    output = tmp_path / "output.jpg"
    Image.new("RGB", (80, 60), "white").save(source)
    output.write_bytes(b"old-result")
    original_save = Image.Image.save

    def fail_staged_save(self, path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and ".fm_watermark_" in str(path):
            with open(path, "wb") as handle:
                handle.write(b"partial")
            raise OSError("disk full")
        return original_save(self, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_staged_save)
    assert not watermark_tool.process_watermark(
        str(source), str(output), wm_type="text", text="WM")
    assert output.read_bytes() == b"old-result"
    assert not any(item.name.startswith(".fm_watermark_")
                   for item in tmp_path.iterdir())


def test_cancel_before_save_preserves_existing_output(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (80, 60), "white").save(source)
    output.write_bytes(b"old-result")

    def cancel(progress, _message):
        if progress >= 80:
            raise InterruptedError

    try:
        process_watermark(
            str(source), str(output), wm_type="text", text="WM",
            progress_cb=cancel)
    except InterruptedError:
        pass
    else:
        raise AssertionError("cancellation must propagate to TaskManager")
    assert output.read_bytes() == b"old-result"
    assert not any(item.name.startswith(".fm_watermark_")
                   for item in tmp_path.iterdir())


def test_exif_orientation_and_invalid_numeric_values_are_safe(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "rotated.jpg"
    output = tmp_path / "output.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (60, 30), "navy").save(source, exif=exif)
    assert process_watermark(
        str(source), str(output), wm_type="text", text="WM",
        font_size="bad", opacity=float("nan"), rotation="bad",
        color="not-a-color", position="center")
    with Image.open(output) as result:
        assert result.size == (30, 60)


def test_same_source_and_output_is_rejected(tmp_path):
    from core.watermark_tool import process_watermark

    source = tmp_path / "source.png"
    Image.new("RGB", (40, 30), "white").save(source)
    original = source.read_bytes()
    assert not process_watermark(
        str(source), str(source), wm_type="text", text="WM")
    assert source.read_bytes() == original
