from utils import hardware_accel


def test_apple_videotoolbox_definition():
    info = hardware_accel.HW_ACCEL_ENCODERS["apple"]

    assert info["hwaccel"] == "videotoolbox"
    assert info["codecs"] == {
        "h264": "h264_videotoolbox",
        "hevc": "hevc_videotoolbox",
    }


def test_macos_prefers_videotoolbox(monkeypatch):
    monkeypatch.setattr(hardware_accel.sys, "platform", "darwin")
    monkeypatch.setattr(hardware_accel, "_detected_accel", None)
    monkeypatch.setattr(
        hardware_accel,
        "_get_ffmpeg_encoders",
        lambda: " V..... h264_videotoolbox Apple VideoToolbox",
    )

    available = hardware_accel.detect_hardware_acceleration()

    assert [item["key"] for item in available] == ["apple"]
    assert hardware_accel.get_best_hw_accel()["key"] == "apple"


def test_video_converter_uses_videotoolbox_defaults(monkeypatch):
    from types import SimpleNamespace

    from core import video_converter as converter_module

    captured = {}
    monkeypatch.setattr(converter_module, "get_ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(converter_module.VideoConverter, "get_duration", lambda self, path: 1.0)
    monkeypatch.setattr(converter_module.VideoConverter, "_get_video_codec_name", lambda self, path: "h264")
    monkeypatch.setattr(converter_module.VideoConverter, "has_audio_stream", lambda self, path: False)
    monkeypatch.setattr(
        converter_module,
        "detect_hardware_acceleration",
        lambda: [{
            "key": "apple",
            "codecs": hardware_accel.HW_ACCEL_ENCODERS["apple"]["codecs"],
            "hwaccel": "videotoolbox",
        }],
    )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    monkeypatch.setattr(converter_module, "run_ffmpeg", fake_run)
    assert converter_module.VideoConverter()._convert_once(
        "input.mp4", "output.mp4", "mp4", preset="high", hw_accel="apple")

    cmd = captured["cmd"]
    assert ["-hwaccel", "videotoolbox"] == cmd[cmd.index("-hwaccel"):cmd.index("-hwaccel") + 2]
    assert ["-c:v", "h264_videotoolbox"] == cmd[cmd.index("-c:v"):cmd.index("-c:v") + 2]
    assert "-crf" not in cmd
    assert "-preset" not in cmd
