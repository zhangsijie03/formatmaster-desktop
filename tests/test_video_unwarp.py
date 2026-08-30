"""视频反挤压页面和核心比例修复的定向回归测试。"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"


def _prepare_core(monkeypatch, meta, run_impl):
    import core.video_unwarp as video_unwarp

    monkeypatch.setattr(video_unwarp, "get_video_dar", lambda _path: meta)
    monkeypatch.setattr(video_unwarp, "_probe_duration", lambda _path: 2.0)
    monkeypatch.setattr(video_unwarp, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(video_unwarp, "run_ffmpeg", run_impl)
    return video_unwarp


def test_manual_ratio_uses_exact_even_dimensions(tmp_path, monkeypatch):
    captured = {}

    def _run(args, **_kwargs):
        captured["args"] = args
        with open(args[-1], "wb") as stream:
            stream.write(b"converted")
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    video_unwarp = _prepare_core(
        monkeypatch, (640, 480, "4:3", "1:1"), _run)
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"video")

    assert video_unwarp.fix_aspect(str(source), str(output), "16:9")
    assert "scale=864:486,setsar=1" in captured["args"]
    assert output.read_bytes() == b"converted"


def test_non_square_pixels_are_reencoded_even_when_dimensions_match(
        tmp_path, monkeypatch):
    captured = {}

    def _run(args, **_kwargs):
        captured["args"] = args
        with open(args[-1], "wb") as stream:
            stream.write(b"converted")
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    video_unwarp = _prepare_core(
        monkeypatch, (640, 360, "16:9", "2:1"), _run)
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"video")

    assert video_unwarp.fix_aspect(str(source), str(output), "16:9")
    assert "scale=640:360,setsar=1" in captured["args"]
    assert "libx264" in captured["args"]


def test_failed_conversion_preserves_existing_output(tmp_path, monkeypatch):
    def _run(_args, **_kwargs):
        return SimpleNamespace(
            success=False, cancelled=False, error_cn="encode failed")

    video_unwarp = _prepare_core(
        monkeypatch, (640, 480, "4:3", "1:1"), _run)
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"video")
    output.write_bytes(b"previous")

    assert not video_unwarp.fix_aspect(str(source), str(output), "16:9")
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".formatmaster-unwarp-*.mp4"))


def test_invalid_or_excessive_targets_are_rejected(tmp_path, monkeypatch):
    video_unwarp = _prepare_core(
        monkeypatch, (640, 480, "4:3", "1:1"),
        lambda *_args, **_kwargs: None)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    errors = []

    assert not video_unwarp.fix_aspect(
        str(source), str(source), "16:9",
        progress_cb=lambda pct, msg: errors.append((pct, msg)))
    assert "不能与源视频相同" in errors[-1][1]
    assert not video_unwarp.fix_aspect(
        str(source), str(tmp_path / "huge.mp4"), "10000:1",
        progress_cb=lambda pct, msg: errors.append((pct, msg)))
    assert "超过 16384" in errors[-1][1]


def test_real_manual_unwarp_produces_square_pixel_video(tmp_path):
    from core.video_unwarp import fix_aspect, get_video_dar

    source = os.path.join(os.path.dirname(__file__), "_media", "sample.mp4")
    if not os.path.isfile(source):
        return
    output = tmp_path / "manual.mp4"
    assert fix_aspect(source, str(output), "16:9")
    width, height, _dar, sar = get_video_dar(str(output))
    assert width % 2 == 0 and height % 2 == 0
    assert width * 9 == height * 16
    assert sar == "1:1"
