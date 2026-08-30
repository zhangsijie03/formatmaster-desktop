"""“图片压缩”菜单审查后的核心数据安全回归测试。"""

import os

import pytest
from PIL import Image


@pytest.mark.parametrize("mode", ["quality", "target"])
def test_transparent_png_keeps_alpha(tmp_path, mode):
    from core.tools import image_compress, image_compress_to_size

    source = tmp_path / "transparent.png"
    output = tmp_path / f"{mode}.png"
    image = Image.new("RGBA", (80, 60), (20, 40, 60, 0))
    image.putpixel((10, 10), (255, 0, 0, 128))
    image.save(source)

    if mode == "quality":
        assert image_compress(str(source), str(output), quality=60)
    else:
        ok, message, size = image_compress_to_size(
            str(source), str(output), target_kb=100)
        assert ok, message
        assert size <= 100 * 1024

    with Image.open(output) as result:
        assert result.format == "PNG"
        assert "A" in result.getbands()
        assert result.getpixel((0, 0))[3] == 0


def test_exif_orientation_is_applied_to_pixels(tmp_path):
    from core.tools import image_compress

    source = tmp_path / "rotated.jpg"
    output = tmp_path / "result.jpg"
    exif = Image.Exif()
    exif[274] = 6  # 90° clockwise
    Image.new("RGB", (40, 20), "purple").save(source, exif=exif)

    assert image_compress(str(source), str(output), quality=80)
    with Image.open(output) as result:
        assert result.size == (20, 40)


@pytest.mark.parametrize(
    ("suffix", "expected_format"), [(".bmp", "BMP"), (".tiff", "TIFF")])
def test_target_mode_writes_matching_container(tmp_path, suffix, expected_format):
    from core.tools import image_compress_to_size

    source = tmp_path / "source.png"
    output = tmp_path / f"result{suffix}"
    Image.new("RGB", (120, 90), "navy").save(source)
    ok, message, _size = image_compress_to_size(
        str(source), str(output), target_kb=100)
    assert ok, message
    with Image.open(output) as result:
        assert result.format == expected_format


def test_unreachable_target_is_failure_and_preserves_old_output(
        monkeypatch, tmp_path):
    from core import tools

    source = tmp_path / "source.png"
    output = tmp_path / "result.png"
    Image.new("RGB", (4, 4), "red").save(source)
    output.write_bytes(b"old-result")
    original_save = Image.Image.save

    def oversized_save(self, fp, *args, **kwargs):
        if hasattr(fp, "write"):
            fp.write(b"x" * 2048)
            return None
        return original_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", oversized_save)
    ok, message, size = tools.image_compress_to_size(
        str(source), str(output), target_kb=1)
    assert not ok
    assert "无法压至" in message
    assert size > 1024
    assert output.read_bytes() == b"old-result"


def test_quality_mode_failure_preserves_old_output(monkeypatch, tmp_path):
    from core import tools

    source = tmp_path / "source.jpg"
    output = tmp_path / "result.jpg"
    Image.new("RGB", (40, 30), "green").save(source)
    output.write_bytes(b"old-result")

    def failing_save(self, path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and ".fm_image_compress_" in str(path):
            with open(path, "wb") as handle:
                handle.write(b"partial")
            raise OSError("disk full")
        return original_save(self, path, *args, **kwargs)

    original_save = Image.Image.save
    monkeypatch.setattr(Image.Image, "save", failing_save)
    assert not tools.image_compress(str(source), str(output), quality=60)
    assert output.read_bytes() == b"old-result"
    assert not any(
        item.name.startswith(".fm_image_compress_") for item in tmp_path.iterdir())


def test_target_loop_can_be_cancelled(tmp_path):
    from core.tools import image_compress_to_size

    source = tmp_path / "source.png"
    output = tmp_path / "result.png"
    Image.effect_noise((1200, 900), 100).save(source)

    def cancel(progress, _message):
        if progress > 30:
            raise InterruptedError

    with pytest.raises(InterruptedError):
        image_compress_to_size(
            str(source), str(output), target_kb=1, progress_cb=cancel)
    assert not output.exists()
