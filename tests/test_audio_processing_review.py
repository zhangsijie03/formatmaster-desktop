"""“音频处理”菜单审查后的关键行为回归测试。"""

import os
from types import SimpleNamespace


def test_parse_time_rejects_invalid_values():
    from gui_qt.panels.audio_trim_panel import parse_time

    assert parse_time("01:02:03.5") == 3723.5
    assert parse_time("90:05") == 5405
    assert parse_time("12.5") == 12.5
    assert parse_time("00:60") is None
    assert parse_time("1:70:00") is None
    assert parse_time("-1") is None
    assert parse_time("abc") is None
    assert parse_time("") is None


def test_remove_silence_repeats_for_internal_sections(monkeypatch):
    from core import audio_tools

    captured = {}

    def fake_run(_src, _dst, chain, _label, *_args):
        captured["chain"] = chain
        return True

    monkeypatch.setattr(audio_tools, "_run_ffmpeg", fake_run)
    assert audio_tools.remove_silence("in.wav", "out.m4a", min_silence=0.7)
    assert "stop_periods=-1" in captured["chain"]
    assert "start_duration=0.70" in captured["chain"]
    assert "stop_duration=0.70" in captured["chain"]


def test_pitch_uses_source_sample_rate(monkeypatch):
    from core import audio_tools

    captured = {}
    monkeypatch.setattr(
        audio_tools, "get_ffprobe_raw",
        lambda *_args, **_kwargs: {
            "streams": [{"codec_type": "audio", "sample_rate": "48000"}]
        })

    def fake_run(_src, _dst, chain, _label, *_args):
        captured["chain"] = chain
        return True

    monkeypatch.setattr(audio_tools, "_run_ffmpeg", fake_run)
    assert audio_tools.audio_pitch("in.wav", "out.m4a", semitones=12)
    assert "asetrate=96000" in captured["chain"]
    assert "aresample=48000" in captured["chain"]


def test_music_extraction_rejects_mono_input(monkeypatch):
    from core import audio_tools

    messages = []
    monkeypatch.setattr(
        audio_tools, "get_ffprobe_raw",
        lambda *_args, **_kwargs: {
            "streams": [{"codec_type": "audio", "channels": 1}]
        })
    monkeypatch.setattr(
        audio_tools, "_run_ffmpeg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mono input must not invoke FFmpeg")))

    assert not audio_tools.extract_music(
        "mono.wav", "music.m4a", progress_cb=lambda _pct, msg: messages.append(msg))
    assert messages and "立体声" in messages[-1]


def test_audio_parameters_are_finite_and_clamped(monkeypatch):
    from core import audio_tools

    chains = []
    monkeypatch.setattr(
        audio_tools, "_run_ffmpeg",
        lambda _src, _dst, chain, _label, *_args: chains.append(chain) or True)

    assert audio_tools.denoise("in", "out", strength="invalid")
    assert chains[-1] == "afftdn=nf=-25"
    assert audio_tools.normalize("in", "out", target_lufs=float("nan"))
    assert "I=-14" in chains[-1]
    assert audio_tools.audio_compress(
        "in", "out", threshold=float("nan"), ratio=float("inf"))
    assert "threshold=-20dB" in chains[-1]
    assert "ratio=4.0" in chains[-1]
    assert audio_tools.audio_equalizer("in", "out", low=99)
    assert "g=+12.0" in chains[-1]


def test_audio_filter_atomic_output_preserves_old_file_on_failure(
        monkeypatch, tmp_path):
    from core import audio_tools

    source = tmp_path / "source.wav"
    output = tmp_path / "result.m4a"
    source.write_bytes(b"source")
    output.write_bytes(b"old")
    monkeypatch.setattr(audio_tools, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_tools, "_duration_of", lambda _path: 1.0)

    def fail_run(cmd, **_kwargs):
        # FFmpeg 即使留下半成品，失败路径也必须清掉而不覆盖旧结果。
        with open(cmd[-1], "wb") as handle:
            handle.write(b"partial")
        return SimpleNamespace(success=False, cancelled=False, error_cn="failed")

    monkeypatch.setattr(audio_tools, "run_ffmpeg", fail_run)
    assert not audio_tools._run_ffmpeg(
        str(source), str(output), "volume=1", "test")
    assert output.read_bytes() == b"old"
    assert not any(p.name.startswith(".fm_audio_fx_") for p in tmp_path.iterdir())


def test_audio_filter_atomic_output_replaces_after_success(monkeypatch, tmp_path):
    from core import audio_tools

    source = tmp_path / "source.wav"
    output = tmp_path / "result.m4a"
    source.write_bytes(b"source")
    output.write_bytes(b"old")
    monkeypatch.setattr(audio_tools, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(audio_tools, "_duration_of", lambda _path: 1.0)

    def success_run(cmd, **_kwargs):
        assert os.path.abspath(cmd[-1]) != os.path.abspath(output)
        with open(cmd[-1], "wb") as handle:
            handle.write(b"complete")
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    monkeypatch.setattr(audio_tools, "run_ffmpeg", success_run)
    assert audio_tools._run_ffmpeg(
        str(source), str(output), "volume=1", "test")
    assert output.read_bytes() == b"complete"


def test_trim_rejects_negative_start_before_running_ffmpeg(monkeypatch, tmp_path):
    from core import audio_trimmer

    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(audio_trimmer, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        audio_trimmer, "run_ffmpeg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid parameters must not invoke FFmpeg")))

    assert not audio_trimmer.trim_audio(
        str(source), str(tmp_path / "out.wav"), start_sec=-1, end_sec=1)
