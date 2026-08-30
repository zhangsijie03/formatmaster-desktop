"""第二轮整体审查修复的跨模块回归测试。"""

import hashlib
import io
import os
import zipfile
from types import SimpleNamespace

import pytest


def test_app_update_requires_matching_sha256(monkeypatch):
    from core import app_updater

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("FormatMaster.exe", b"MZ")
    payload = buf.getvalue()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self.sent:
                return b""
            self.sent = True
            return payload

    monkeypatch.setattr(
        app_updater.urllib.request, "urlopen", lambda *_a, **_k: Response())
    with pytest.raises(RuntimeError, match="SHA-256"):
        app_updater.download_update(["https://mirror.invalid/update.zip"], "0" * 64)

    path = app_updater.download_update(
        ["https://mirror.invalid/update.zip"],
        hashlib.sha256(payload).hexdigest())
    try:
        assert os.path.isfile(path)
    finally:
        os.remove(path)


def test_app_update_checksum_parser_matches_exact_asset():
    from core.app_updater import _parse_checksum

    wanted = "a" * 64
    text = f"{'b' * 64}  other.zip\n{wanted} *FormatMaster.zip\n"
    assert _parse_checksum(text, "FormatMaster.zip") == wanted
    assert _parse_checksum(text, "Format.zip") is None


def test_ytdlp_rejects_bad_payload_and_preserves_old(monkeypatch, tmp_path):
    from core import tool_updater

    payload = b"<html>not a binary</html>"
    old = tmp_path / tool_updater.YTDLP_EXE
    old.write_bytes(b"known-good-old")

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self.sent:
                return b""
            self.sent = True
            return payload

    monkeypatch.setattr(
        tool_updater, "fetch_latest_ytdlp_version", lambda: "2099.01.01")
    monkeypatch.setattr(
        tool_updater, "_fetch_ytdlp_checksum",
        lambda _tag: hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        tool_updater.urllib.request, "urlopen", lambda *_a, **_k: Response())
    monkeypatch.setattr(
        tool_updater, "get_writable_bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        tool_updater, "_validate_ytdlp_binary", lambda *_a: False)

    ok, message = tool_updater.download_ytdlp()
    assert not ok
    assert "健康检查" in message
    assert old.read_bytes() == b"known-good-old"


def test_ytdlp_replace_failure_preserves_old(monkeypatch, tmp_path):
    from core import tool_updater

    payload = b"valid replacement"
    old = tmp_path / tool_updater.YTDLP_EXE
    old.write_bytes(b"known-good-old")

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self.sent:
                return b""
            self.sent = True
            return payload

    monkeypatch.setattr(
        tool_updater, "fetch_latest_ytdlp_version", lambda: "2099.01.01")
    monkeypatch.setattr(
        tool_updater, "_fetch_ytdlp_checksum",
        lambda _tag: hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        tool_updater.urllib.request, "urlopen", lambda *_a, **_k: Response())
    monkeypatch.setattr(
        tool_updater, "get_writable_bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        tool_updater, "_validate_ytdlp_binary", lambda *_a: True)
    real_replace = tool_updater.os.replace

    def fail_only_install(source, target):
        if target == str(old):
            raise OSError("busy")
        return real_replace(source, target)

    monkeypatch.setattr(tool_updater.os, "replace", fail_only_install)
    ok, message = tool_updater.download_ytdlp()
    assert not ok
    assert "无法替换" in message
    assert old.read_bytes() == b"known-good-old"


def test_ffmpeg_pair_install_rolls_back_both_tools(monkeypatch, tmp_path):
    from utils import ffmpeg_manager

    suffix = ".exe" if os.name == "nt" else ""
    names = ("ffmpeg" + suffix, "ffprobe" + suffix)
    for name in names:
        (tmp_path / name).write_bytes(("old-" + name).encode())

    def fake_extract(_archive, stage_dir):
        for name in names:
            path = os.path.join(stage_dir, name)
            with open(path, "wb") as stream:
                stream.write(("new-" + name).encode())

    monkeypatch.setattr(ffmpeg_manager, "_extract_ffmpeg", fake_extract)
    monkeypatch.setattr(ffmpeg_manager, "_validate_ffmpeg_pair", lambda _dir: True)
    real_replace = ffmpeg_manager.os.replace

    def fail_second_staged_install(source, target):
        if (os.path.basename(source) == names[1]
                and ".ffmpeg-stage-" in os.path.dirname(source)):
            raise OSError("simulated second-tool failure")
        return real_replace(source, target)

    monkeypatch.setattr(
        ffmpeg_manager.os, "replace", fail_second_staged_install)
    with pytest.raises(OSError, match="second-tool"):
        ffmpeg_manager._install_ffmpeg_archive(
            str(tmp_path / "unused.zip"), str(tmp_path))

    for name in names:
        assert (tmp_path / name).read_bytes() == ("old-" + name).encode()


def test_video_probe_keeps_fallback_resolution_and_audio_flags(monkeypatch):
    from core import video_merge

    streams = {
        "a": [("h264", 1280, 720, True)],
        "b": [("hevc", 640, 360, True)],
    }
    monkeypatch.setattr(
        video_merge, "_video_streams", lambda path: streams[path])
    assert video_merge._probe_compatible(["a", "b"]) == (
        False, (1280, 720), (True, True))


def test_video_reencode_preserves_audio_and_fills_missing_track(monkeypatch):
    from core import video_merge

    captured = {}
    monkeypatch.setattr(video_merge, "_duration_of", lambda _path: 2.5)

    def fake_run(args, *_rest):
        captured["args"] = args
        return True

    monkeypatch.setattr(video_merge, "_run_ffmpeg", fake_run)
    assert video_merge._merge_reencode(
        ["a.mp4", "b.mp4"], "out.mp4", (1280, 720),
        (True, False), None, None)
    command = " ".join(captured["args"])
    assert "scale=1280:720" in command
    assert "anullsrc=" in command
    assert "[outa]" in command
    assert "-an" not in captured["args"]


def test_merge_helpers_do_not_delete_preexisting_files(monkeypatch, tmp_path):
    from core import audio_tools, video_merge

    video_marker = tmp_path / "_fm_merge_list.txt"
    audio_marker = tmp_path / "_fm_aconcat.txt"
    video_marker.write_text("video user data", encoding="utf-8")
    audio_marker.write_text("audio user data", encoding="utf-8")
    inputs = [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")]
    monkeypatch.setattr(video_merge, "_duration_of", lambda _path: 1.0)
    monkeypatch.setattr(video_merge, "_run_ffmpeg", lambda *_a: True)
    assert video_merge._merge_copy(
        inputs, str(tmp_path / "out.mp4"), None, None)

    monkeypatch.setattr(audio_tools, "_duration_of", lambda _path: 1.0)
    monkeypatch.setattr(audio_tools, "get_ffmpeg_path", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(
        audio_tools, "run_ffmpeg",
        lambda *_a, **_k: SimpleNamespace(
            success=True, cancelled=False, error_cn=""))
    assert audio_tools.concat_audio(
        [str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")],
        str(tmp_path / "out.m4a"))
    assert video_marker.read_text(encoding="utf-8") == "video user data"
    assert audio_marker.read_text(encoding="utf-8") == "audio user data"


def test_audio_concat_escapes_apostrophe(monkeypatch, tmp_path):
    from core import audio_tools

    captured = {}
    source = tmp_path / "singer's-a.mp3"
    other = tmp_path / "b.mp3"
    source.write_bytes(b"")
    other.write_bytes(b"")
    monkeypatch.setattr(audio_tools, "_duration_of", lambda _path: 1.0)
    monkeypatch.setattr(audio_tools, "get_ffmpeg_path", lambda: "/fake/ffmpeg")

    def fake_run(command, **_kwargs):
        list_path = command[command.index("-i") + 1]
        captured["text"] = open(list_path, encoding="utf-8").read()
        return SimpleNamespace(success=True, cancelled=False, error_cn="")

    monkeypatch.setattr(audio_tools, "run_ffmpeg", fake_run)
    assert audio_tools.concat_audio(
        [str(source), str(other)], str(tmp_path / "joined.m4a"))
    assert "singer'\\''s-a.mp3" in captured["text"]


def test_legacy_video_copy_concat_executes(monkeypatch):
    from core import video_converter, video_tools

    called = {}
    monkeypatch.setattr(
        video_converter, "get_ffmpeg_path", lambda: "/fake/ffmpeg")

    def fake_merge(paths, output, progress_cb, cancel_check):
        called.update(paths=paths, output=output, cancelled=cancel_check())
        return True

    monkeypatch.setattr(video_tools, "merge_videos", fake_merge)
    assert video_converter.VideoConverter().concat(
        ["a.mp4", "b.mp4"], "out.mp4", copy_mode=True)
    assert called == {
        "paths": ["a.mp4", "b.mp4"],
        "output": "out.mp4",
        "cancelled": False,
    }
