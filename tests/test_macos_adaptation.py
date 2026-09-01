import os
import plistlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import build
from gui_qt import context_menu
from core import (audio_utils, doc_misc, doc_office_pdf, ebook_converter,
                  tool_updater, video_downloader)
from utils import config


def test_context_menu_is_safe_outside_windows(monkeypatch):
    monkeypatch.setattr(context_menu.sys, "platform", "darwin")

    assert context_menu.install() == "Windows only"
    assert context_menu.uninstall() == "Windows only"
    assert context_menu.installed() is False


def test_macos_os_info_uses_user_facing_version(monkeypatch):
    module_path = (Path(__file__).parents[1] / "gui_qt" / "components"
                   / "sysinfo.py")
    spec = importlib.util.spec_from_file_location(
        "_fm_sysinfo_test", module_path)
    sysinfo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sysinfo)
    monkeypatch.setattr(sysinfo.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sysinfo.platform, "release", lambda: "24.5.0")
    monkeypatch.setattr(
        sysinfo.platform, "mac_ver", lambda: ("15.5", ("", "", ""), ""))

    assert sysinfo.os_info() == {
        "system": "macOS",
        "release": "15.5",
        "build": "",
        "arch": sysinfo.platform.machine(),
    }


def test_ytdlp_path_uses_native_name_and_path(monkeypatch, tmp_path):
    native = tmp_path / tool_updater.YTDLP_EXE
    native.write_bytes(b"fake")
    monkeypatch.setattr(tool_updater, "get_writable_bin_dir", lambda: str(tmp_path))

    assert tool_updater._ytdlp_exe_path() == str(native)


def test_ytdlp_download_uses_macos_release_asset(monkeypatch):
    monkeypatch.setattr(tool_updater, "YTDLP_DOWNLOAD_ASSET", "yt-dlp_macos")

    assert tool_updater._ytdlp_download_url("2026.08.19").endswith(
        "/2026.08.19/yt-dlp_macos")


def test_video_downloader_reuses_native_ytdlp_lookup(monkeypatch):
    monkeypatch.setattr(tool_updater, "_ytdlp_exe_path",
                        lambda: "/tmp/yt-dlp")

    assert video_downloader._find_ytdlp_exe() == "/tmp/yt-dlp"


def test_video_downloader_cli_uses_platform_creationflags(monkeypatch):
    captured = {}
    monkeypatch.setattr(video_downloader, "_find_ytdlp_exe",
                        lambda: "/tmp/yt-dlp")

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"formats": [{"format_id": "18", "ext": "mp4"}],'
                   '"title": "demo"}',
            stderr="",
        )

    monkeypatch.setattr(video_downloader.subprocess, "run", fake_run)
    formats, title, _thumbnail, _playlist = (
        video_downloader.VideoDownloader()._get_formats_cli("https://example.com"))

    assert formats[0]["format_id"] == "18"
    assert title == "demo"
    expected_flags = 0x08000000 if sys.platform == "win32" else 0
    assert captured["creationflags"] == expected_flags


def test_audio_waveform_uses_cross_platform_creationflags(monkeypatch):
    captured = {}
    monkeypatch.setattr(audio_utils, "get_ffmpeg_path", lambda: "/tmp/ffmpeg")

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)
        return type("Result", (), {"returncode": 0, "stdout": b""})()

    monkeypatch.setattr(audio_utils.subprocess, "run", fake_run)
    assert audio_utils._run_ffmpeg_pcm("demo.mp3") == (b"", 8000)
    expected_flags = 0x08000000 if os.name == "nt" else 0
    assert captured["creationflags"] == expected_flags


def test_frozen_macos_tools_are_written_to_user_support_dir(monkeypatch, tmp_path):
    support = tmp_path / "Application Support"
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "_MEIPASS", str(tmp_path / "_internal"),
                        raising=False)
    monkeypatch.setattr(config.sys, "executable",
                        str(tmp_path / "FormatMaster.app" / "Contents" /
                            "MacOS" / "FormatMaster"), raising=False)
    monkeypatch.setattr(config, "get_app_support_dir", lambda: str(support))
    config._WRITABLE_BIN_DIR_CACHE = None
    try:
        path = config.get_writable_bin_dir()
        assert path == str(support / config.APP_DATA_DIR_NAME / "bin")
        assert ".app" not in path
    finally:
        config._WRITABLE_BIN_DIR_CACHE = None


def test_frozen_user_data_keeps_legacy_localized_directory(monkeypatch, tmp_path):
    """Existing localized data remains readable after the stable-path fix."""
    support = tmp_path / "Application Support"
    legacy = support / "格式大师" / "data"
    legacy.mkdir(parents=True)
    (legacy / "user_prefs.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "get_app_support_dir", lambda: str(support))

    assert config.get_user_data_dir() == str(legacy)


def test_legacy_ppt_to_txt_uses_libreoffice_on_macos(monkeypatch, tmp_path):
    source = tmp_path / "slides.ppt"
    target = tmp_path / "renamed.txt"
    source.write_bytes(b"fake ppt")
    captured = {}

    monkeypatch.setattr(doc_misc.sys, "platform", "darwin")
    monkeypatch.setattr(doc_office_pdf, "_find_soffice",
                        lambda: "/usr/bin/soffice")

    def fake_run(_cmd, **kwargs):
        captured.update(kwargs)
        (tmp_path / "slides.txt").write_text("slide text", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(doc_misc.subprocess, "run", fake_run)
    assert doc_misc.DocMiscMixin()._ppt_to_txt(
        str(source), str(target), None) is True
    assert target.read_text(encoding="utf-8") == "slide text"
    assert captured["creationflags"] == 0


def test_calibre_path_includes_macos_app_bundle(monkeypatch):
    expected = "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    monkeypatch.setattr(ebook_converter.sys, "platform", "darwin")
    monkeypatch.setattr(ebook_converter.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        ebook_converter.os.path,
        "isfile",
        lambda path: path == expected,
    )

    assert ebook_converter._find_calibre_convert() == expected


def test_macos_bundle_metadata_registers_supported_files(monkeypatch, tmp_path):
    app_contents = tmp_path / "格式大师.app" / "Contents"
    app_contents.mkdir(parents=True)
    info_path = app_contents / "Info.plist"
    with info_path.open("wb") as stream:
        plistlib.dump({"CFBundleName": "FormatMaster"}, stream)

    monkeypatch.setattr(build.sys, "platform", "darwin")
    assert build._configure_macos_bundle(str(tmp_path)) is True

    with info_path.open("rb") as stream:
        info = plistlib.load(stream)
    document_type = info["CFBundleDocumentTypes"][0]
    assert document_type["CFBundleTypeRole"] == "Editor"
    assert "docx" in document_type["CFBundleTypeExtensions"]
    assert "dps" in document_type["CFBundleTypeExtensions"]
    assert "epub" in document_type["CFBundleTypeExtensions"]
    assert "md" in document_type["CFBundleTypeExtensions"]


def test_macos_release_pipeline_builds_signed_notarized_dmg(
        monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    app = dist / "格式大师.app"
    app.mkdir(parents=True)
    out_root = tmp_path / "release"
    calls = []

    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    # The test simulates macOS on every CI host; Windows may forbid symlink
    # creation without Developer Mode even though production runs on macOS.
    monkeypatch.setattr(build.os, "symlink", lambda *_args, **_kwargs: None)

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    dmg = build._finalize_macos_release(
        str(dist), str(out_root), sign_identity="Developer ID Test",
        notarize_profile="FormatMasterNotary", make_dmg=True)

    assert dmg == str(out_root / "格式大师-macOS.dmg")
    assert any("--sign" in call and "Developer ID Test" in call
               for call in calls)
    assert any(call[0].endswith("hdiutil") and "create" in call
               for call in calls)
    assert any(call[0].endswith("xcrun") and "notarytool" in call
               and "submit" in call for call in calls)
    assert any(call[0].endswith("xcrun") and "stapler" in call
               and "validate" in call for call in calls)


def test_macos_adhoc_signing_preserves_nested_executables(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_require_macos_tool",
                        lambda _name: "/usr/bin/codesign")
    monkeypatch.setattr(
        build, "_run_macos_release_command",
        lambda cmd, label: calls.append((cmd, label)))

    build._sign_macos_app_adhoc("/tmp/FormatMaster.app")

    sign_command = calls[0][0]
    verify_command = calls[1][0]
    assert "--deep" not in sign_command
    assert "--force" in sign_command
    assert "--deep" in verify_command
    assert "--strict" in verify_command


def test_macos_dmg_retries_only_transient_resource_busy(monkeypatch, tmp_path):
    app = tmp_path / "FormatMaster.app"
    app.mkdir()
    output = tmp_path / "FormatMaster.dmg"
    attempts = []

    monkeypatch.setattr(build.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(build.os, "symlink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(build.time, "sleep", lambda seconds: attempts.append(seconds))

    def fake_run(_cmd, **_kwargs):
        if len(attempts) < 2:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="Resource busy")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build.subprocess, "run", fake_run)

    assert build._create_macos_dmg(str(app), str(output)) == str(output)
    assert attempts == [2, 4]


def test_macos_notarization_requires_signing_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(build.sys, "platform", "darwin")
    try:
        build._finalize_macos_release(
            str(tmp_path), str(tmp_path), notarize_profile="profile",
            make_dmg=True)
    except RuntimeError as exc:
        assert "sign-identity" in str(exc)
    else:
        raise AssertionError("未签名构建不应进入 notarization")


def test_macos_release_options_fail_before_build(monkeypatch):
    monkeypatch.setattr(build.sys, "platform", "darwin")
    try:
        build._validate_macos_release_options(
            sign_identity="Developer ID Test",
            notarize_profile="profile",
            make_dmg=False)
    except RuntimeError as exc:
        assert "--dmg" in str(exc)
    else:
        raise AssertionError("公证参数缺少 --dmg 时应在构建前失败")


def test_macos_release_reports_tool_launch_errors(monkeypatch):
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/usr/bin/tool")

    def fail_run(*_args, **_kwargs):
        raise OSError("工具链不可用")

    monkeypatch.setattr(build.subprocess, "run", fail_run)
    try:
        build._run_macos_release_command(["/usr/bin/codesign"], "macOS 代码签名")
    except RuntimeError as exc:
        assert "无法启动" in str(exc)
        assert "工具链不可用" in str(exc)
    else:
        raise AssertionError("发布工具启动失败应转换为可读的 RuntimeError")
