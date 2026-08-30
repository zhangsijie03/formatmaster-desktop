"""“图片拼接 / PDF 相册”核心正确性与数据安全回归测试。"""

import os

from PIL import Image


def test_vertical_merge_uses_resized_heights_without_cropping(tmp_path):
    from core.image_album import merge_vertical

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    output = tmp_path / "vertical.png"
    Image.new("RGB", (100, 100), "red").save(first)
    Image.new("RGB", (50, 100), "blue").save(second)

    assert merge_vertical([str(first), str(second)], str(output), gap=10)
    with Image.open(output) as result:
        assert result.size == (100, 310)
        assert result.getpixel((50, 50)) == (255, 0, 0)
        assert result.getpixel((50, 105)) == (255, 255, 255)
        assert result.getpixel((50, 250)) == (0, 0, 255)


def test_horizontal_merge_uses_resized_widths_without_cropping(tmp_path):
    from core.image_album import merge_horizontal

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    output = tmp_path / "horizontal.png"
    Image.new("RGB", (100, 50), "red").save(first)
    Image.new("RGB", (50, 100), "blue").save(second)

    assert merge_horizontal([str(first), str(second)], str(output), gap=10)
    with Image.open(output) as result:
        assert result.size == (260, 100)
        assert result.getpixel((100, 50)) == (255, 0, 0)
        assert result.getpixel((205, 50)) == (255, 255, 255)
        assert result.getpixel((230, 50)) == (0, 0, 255)


def test_exif_orientation_and_transparency_use_visible_white_background(tmp_path):
    from core.image_album import merge_vertical

    rotated = tmp_path / "rotated.jpg"
    transparent = tmp_path / "transparent.png"
    rotated_output = tmp_path / "rotated_out.png"
    transparent_output = tmp_path / "transparent_out.png"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (60, 30), "navy").save(rotated, exif=exif)
    Image.new("RGBA", (20, 10), (255, 0, 0, 0)).save(transparent)

    assert merge_vertical([str(rotated)], str(rotated_output), gap=0)
    assert merge_vertical([str(transparent)], str(transparent_output), gap=0)
    with Image.open(rotated_output) as result:
        assert result.size == (30, 60)
    with Image.open(transparent_output) as result:
        assert result.getpixel((5, 5)) == (255, 255, 255)


def test_invalid_inputs_and_same_path_preserve_source(tmp_path):
    from core.image_album import merge_vertical, to_pdf

    source = tmp_path / "source.png"
    Image.new("RGB", (20, 10), "white").save(source)
    original = source.read_bytes()
    messages = []
    callback = lambda pct, msg: messages.append((pct, msg))

    assert not merge_vertical([], str(tmp_path / "empty.png"), progress_callback=callback)
    assert not merge_vertical(str(source), str(tmp_path / "string.png"),
                              progress_callback=callback)
    assert not merge_vertical([str(source)], str(source), progress_callback=callback)
    assert not merge_vertical([str(source)], str(tmp_path / "bad.png"), gap=-1,
                              progress_callback=callback)
    assert not to_pdf([str(source)], str(tmp_path / "bad.pdf"), "unknown", callback)
    assert source.read_bytes() == original
    assert all(not path.exists() for path in (
        tmp_path / "empty.png", tmp_path / "string.png",
        tmp_path / "bad.png", tmp_path / "bad.pdf"))
    assert any(pct < 0 for pct, _message in messages)


def test_failure_and_last_moment_cancel_preserve_existing_output(monkeypatch,
                                                                  tmp_path):
    from core import image_album

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (20, 10), "red").save(source)
    output.write_bytes(b"old-result")
    original_save = Image.Image.save

    def fail_staged_save(self, path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and ".fm_image_merge_" in str(path):
            raise OSError("disk full")
        return original_save(self, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_staged_save)
    assert not image_album.merge_vertical([str(source)], str(output))
    assert output.read_bytes() == b"old-result"
    monkeypatch.setattr(Image.Image, "save", original_save)

    def cancel(progress, _message):
        if progress >= 95:
            raise InterruptedError

    try:
        image_album.merge_vertical([str(source)], str(output),
                                   progress_callback=cancel)
    except InterruptedError:
        pass
    else:
        raise AssertionError("cancellation must propagate to TaskManager")
    assert output.read_bytes() == b"old-result"
    assert not any(path.name.startswith(".fm_image_merge_")
                   for path in tmp_path.iterdir())


def test_pdf_modes_create_all_pages_atomically(tmp_path):
    from pypdf import PdfReader
    from core.image_album import to_pdf

    files = []
    for index, size in enumerate(((80, 60), (40, 100))):
        path = tmp_path / f"page_{index}.png"
        Image.new("RGB", size, (index * 100, 50, 200)).save(path)
        files.append(str(path))
    for mode in ("A4", "original"):
        output = tmp_path / f"album_{mode}.pdf"
        assert to_pdf(files, str(output), mode)
        assert len(PdfReader(str(output)).pages) == 2
