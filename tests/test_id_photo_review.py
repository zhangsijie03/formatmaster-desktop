"""“证件照换底色”菜单审查后的核心正确性与数据安全测试。"""

import os

import numpy as np
from PIL import Image


def test_transparent_output_keeps_alpha_and_requested_size(tmp_path):
    from core.id_photo import TRANSPARENT_KEY, change_background

    source = tmp_path / "source.png"
    output = tmp_path / "transparent.png"
    Image.new("RGB", (80, 60), "white").save(source)
    assert change_background(
        str(source), str(output), TRANSPARENT_KEY,
        use_ai=False, size=(40, 50))
    with Image.open(output) as result:
        assert result.size == (40, 50)
        assert "A" in result.getbands()


def test_exif_orientation_is_applied_before_processing(tmp_path):
    from core.id_photo import change_background

    source = tmp_path / "rotated.jpg"
    output = tmp_path / "output.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (60, 30), "navy").save(source, exif=exif)
    assert change_background(
        str(source), str(output), "白底", use_ai=False)
    with Image.open(output) as result:
        assert result.size == (30, 60)


def test_invalid_params_and_same_path_preserve_existing_data(tmp_path):
    from core.id_photo import change_background

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (40, 30), "white").save(source)
    original_source = source.read_bytes()
    output.write_bytes(b"old-result")
    assert not change_background(
        str(source), str(output), "白底", use_ai=False, size=(0, 20))
    assert output.read_bytes() == b"old-result"
    assert not change_background(str(source), str(source), "白底", use_ai=False)
    assert source.read_bytes() == original_source


def test_save_failure_and_cancel_preserve_existing_output(monkeypatch, tmp_path):
    from core import id_photo

    source = tmp_path / "source.png"
    output = tmp_path / "output.png"
    Image.new("RGB", (40, 30), "white").save(source)
    output.write_bytes(b"old-result")
    original_save = Image.Image.save

    def fail_staged_save(self, path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and ".fm_idphoto_" in str(path):
            raise OSError("disk full")
        return original_save(self, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_staged_save)
    assert not id_photo.change_background(
        str(source), str(output), "白底", use_ai=False)
    assert output.read_bytes() == b"old-result"
    monkeypatch.setattr(Image.Image, "save", original_save)

    def cancel(progress, _message):
        if progress >= 95:
            raise InterruptedError

    try:
        id_photo.change_background(
            str(source), str(output), "白底", cancel, use_ai=False)
    except InterruptedError:
        pass
    else:
        raise AssertionError("cancellation must propagate to TaskManager")
    assert output.read_bytes() == b"old-result"
    assert not any(item.name.startswith(".fm_idphoto_")
                   for item in tmp_path.iterdir())


def test_size_conversion_rejects_non_finite_and_huge_values():
    from core.id_photo import size_to_px

    assert size_to_px("nan", "10", "px") is None
    assert size_to_px("inf", "10", "px") is None
    assert size_to_px("100000", "10", "cm") is None


def test_a6_default_layout_matches_advertised_tile_counts():
    from core.id_photo import PAPER_SIZES, PHOTO_SIZES, layout_print

    for size_key, expected in (("1寸", 16), ("2寸", 9)):
        photo = Image.new("RGB", PHOTO_SIZES[size_key], "black")
        sheet = layout_print(photo, PAPER_SIZES["A6"], dpi=300)
        black_pixels = int(np.all(np.asarray(sheet) == 0, axis=2).sum())
        assert black_pixels == expected * photo.width * photo.height
        photo.close()
        sheet.close()
