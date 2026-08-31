"""Release workflow contract tests.

These tests intentionally inspect the workflow text instead of importing a
YAML parser. CI must be able to run them before optional developer tooling is
installed, and the assertions focus on release behavior that would be
dangerous to remove accidentally.
"""
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_macos_bundle_version_mapping_supports_prereleases():
    from build import _macos_bundle_versions

    assert _macos_bundle_versions("1.5.0") == ("1.5.0", "1.5.0")
    assert _macos_bundle_versions("1.5.0-beta.2") == ("1.5.0", "1.5.0b2")
    assert _macos_bundle_versions("1.5.0-rc.1") == ("1.5.0", "1.5.0fc1")


def test_release_workflow_targets_both_macos_architectures():
    text = _workflow_text()
    assert "arch: arm64" in text
    assert "runner: macos-14" in text
    assert "arch: x86_64" in text
    assert "runner: macos-15-intel" in text


def test_release_workflow_blocks_builds_until_source_gate_passes():
    text = _workflow_text()
    assert "verify-source:" in text
    assert text.count("needs: verify-source") == 2
    assert "python -m ruff check" in text
    assert "python -m pytest -q" in text
    assert "scripts/release_preflight.py --tag" in text
    assert "--require-clean" in text
    assert 'NOTES="docs/releases/${RELEASE_TAG}.md"' in text
    assert "--require-hashes -r requirements.lock" in text


def test_release_workflow_builds_and_validates_dmg():
    text = _workflow_text()
    assert "python build.py --dmg" in text
    assert '--sign-identity "$MACOS_SIGN_IDENTITY"' in text
    assert '--notarize-profile "$MACOS_NOTARY_PROFILE"' in text
    assert "hdiutil verify \"$DMG\"" in text
    assert 'codesign --verify --deep --strict "$APP"' in text
    assert 'spctl --assess --type execute' in text
    assert 'xcrun stapler validate "$DMG"' in text
    assert "Print :CFBundleShortVersionString" in text
    assert "Print :FormatMasterReleaseVersion" in text
    assert 'SUFFIX="-unsigned"' in text
    assert "FormatMaster-${VERSION}-macOS-${{ matrix.arch }}${SUFFIX}.dmg" in text


def test_unsigned_macos_fallback_is_explicit_and_clearly_labeled():
    text = _workflow_text()
    assert "unsigned_macos:" in text
    assert "type: boolean" in text
    assert 'if: ${{ inputs.unsigned_macos }}' in text
    assert 'if: ${{ !inputs.unsigned_macos }}' in text
    assert "Signature=adhoc" in text
    assert "*-unsigned.dmg" in text
    assert 'gh release upload "$RELEASE_TAG"' in text


def test_release_workflow_publishes_checksums_and_assets():
    text = _workflow_text()
    assert "sha256sum * > SHA256SUMS.txt" in text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in text
    assert "name '*.dmg'" in text
    assert "windows-x64-portable.zip" in text
    assert "*.spdx.json" in text
    assert "attestations: write" in text
    assert "contents: write" in text
    assert "id-token: write" in text
    assert "gh release create \"$RELEASE_TAG\"" in text
    assert "--clobber" not in text
    assert "禁止覆盖不可变发布资产" in text
    assert "--verify-tag" in text


def test_release_workflow_bundles_runtime_tools():
    text = _workflow_text()
    assert "MACOS_FFMPEG_RELEASE" in text
    assert "WINDOWS_FFMPEG_RELEASE" in text
    assert "YTDLP_VERSION" in text
    assert "ffmpeg_sha256" in text
    assert "YTDLP_WINDOWS_SHA256" in text
    assert "yt-dlp_macos" in text
    assert "yt-dlp.exe" in text
    assert "FFmpeg/FFprobe 未找到" in text
    assert 'test -x "$TOOLS_DIR/ffmpeg"' in text
    assert 'test -x "$TOOLS_DIR/ffprobe"' in text
    assert 'test -x "$TOOLS_DIR/yt-dlp"' in text


def test_public_distribution_requires_platform_signing():
    text = _workflow_text()
    assert "正式发布必须配置 Windows 代码签名证书" in text
    assert "MACOS_CERTIFICATE_P12_BASE64" in text
    assert "WINDOWS_CERTIFICATE_PFX_BASE64" in text
    assert 'grep -F "Authority=Developer ID Application"' in text
    assert "$signtool.FullName verify /pa /v" in text


def test_release_workflow_uses_robust_windows_onedir_zip():
    text = _workflow_text()
    assert "& python -u build.py" in text
    assert "python build.py --onefile" not in text
    assert 'Join-Path $appDir "格式大师.exe"' in text
    assert 'Join-Path $toolsDir "ffmpeg.exe"' in text
    assert 'Join-Path $toolsDir "ffprobe.exe"' in text
    assert 'Join-Path $toolsDir "yt-dlp.exe"' in text
    assert "Windows ZIP 未生成" in text
    assert "记录 Windows 构建预检" in text
    assert "上传 Windows 构建诊断" in text
    assert "python -u build.py" in text
    assert "signtool.FullName sign" in text
    assert "--self-test-package" in text


def test_release_workflow_requires_complete_asset_set_and_notes():
    text = _workflow_text()
    assert 'test -f "FormatMaster-${VERSION}-macOS-arm64.dmg"' in text
    assert 'test -f "FormatMaster-${VERSION}-macOS-x86_64.dmg"' in text
    assert 'test -f "FormatMaster-${VERSION}-windows-x64-portable.zip"' in text
    assert 'NOTES="docs/releases/${RELEASE_TAG}.md"' in text
    assert "Automated multi-platform release" not in text


def test_build_script_configures_utf8_console_before_windows_build():
    text = (ROOT / "build.py").read_text(encoding="utf-8")
    assert "def _configure_console_encoding():" in text
    assert 'reconfigure(encoding="utf-8", errors="replace")' in text
    assert "_configure_console_encoding()" in text
