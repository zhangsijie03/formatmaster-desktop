"""字幕提取页面与核心链路的定向回归测试。"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"


def test_full_region_uses_entire_frame():
    from core.subtitle_extract import _crop_box

    image = SimpleNamespace(size=(1920, 1080))
    assert _crop_box(image, "full", 0.15, None) == (0, 0, 1920, 1080)
    assert _crop_box(image, "bottom", 0.15, None) == (0, 918, 1920, 1080)


def test_extract_stage_progress_is_monotonic(tmp_path, monkeypatch):
    import core.subtitle_extract as subtitle_extract

    monkeypatch.setattr(subtitle_extract, "get_ffmpeg_path", lambda: "ffmpeg")

    def _run(cmd, progress_callback=None, **_kwargs):
        for pct in (0, 50, 100):
            progress_callback(pct, "extract")
        with open(cmd[-1].replace("%05d", "00001"), "wb") as stream:
            stream.write(b"png")
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    monkeypatch.setattr(subtitle_extract, "run_ffmpeg", _run)
    updates = []
    frames = subtitle_extract._extract_frames(
        "source.mp4", str(tmp_path), 1.0, duration=10,
        progress_cb=lambda pct, _msg: updates.append(pct))

    assert len(frames) == 1
    assert updates == [5, 17, 30]


def test_ocr_engine_failure_is_reported(monkeypatch):
    import core.ocr_tool as ocr_tool
    from core.subtitle_extract import _ocr_frames

    monkeypatch.setattr(
        ocr_tool, "_get_engine",
        lambda: (_ for _ in ()).throw(RuntimeError("model missing")))
    updates = []
    assert _ocr_frames(
        ["frame.png"], 1.0, "chi_sim+eng",
        progress_cb=lambda pct, msg: updates.append((pct, msg))) is None
    assert updates == [(-1, "OCR 引擎不可用：model missing")]


def test_existing_srt_survives_atomic_replace_failure(tmp_path, monkeypatch):
    import core.subtitle_extract as subtitle_extract

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    output = tmp_path / "source.srt"
    output.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(subtitle_extract, "_duration_of", lambda _path: 2.0)
    monkeypatch.setattr(
        subtitle_extract, "_extract_frames",
        lambda *_args, **_kwargs: ["frame.png"])
    monkeypatch.setattr(
        subtitle_extract, "_ocr_frames",
        lambda *_args, **_kwargs: [(0.0, 1.0, "字幕")])
    monkeypatch.setattr(
        subtitle_extract.os, "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        subtitle_extract.extract_subtitles(str(source), str(output))
    assert output.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".formatmaster-subtitle-*.srt"))


def test_real_frame_extraction_writes_valid_srt(tmp_path, monkeypatch):
    """真实 FFmpeg 负责抽帧，OCR 结果固定以验证完整写出链路。"""
    import core.subtitle_extract as subtitle_extract

    source = os.path.join(os.path.dirname(__file__), "_media", "sample.mp4")
    if not os.path.isfile(source):
        pytest.skip("sample video unavailable")
    output = tmp_path / "sample.srt"
    monkeypatch.setattr(
        subtitle_extract, "_ocr_frames",
        lambda frames, *_args, **_kwargs: [(0.0, 1.0, f"共 {len(frames)} 帧")])

    assert subtitle_extract.extract_subtitles(
        source, str(output), fps=1.0, region="full", height=0.15)
    text = output.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,000" in text
    assert "共 " in text
