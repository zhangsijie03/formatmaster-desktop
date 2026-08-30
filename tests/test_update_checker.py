from gui_qt import update_checker


class _FakeResponse:
    def __init__(self, chunks, fail_after=None):
        self.headers = {"Content-Length": "7"}
        self._chunks = iter(chunks)
        self._read_count = 0
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, _size):
        if self._fail_after is not None and self._read_count >= self._fail_after:
            raise OSError("模拟下载中断")
        self._read_count += 1
        return next(self._chunks, b"")


def test_download_asset_replaces_destination_atomically(monkeypatch, tmp_path):
    progress = []
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse([b"new", b"data"]),
    )

    destination = tmp_path / "update.zip"
    destination.write_bytes(b"old")
    result = update_checker.download_asset(
        "https://example.test/releases/update.zip",
        str(tmp_path),
        lambda done, total: progress.append((done, total)),
    )

    assert result == str(destination)
    assert destination.read_bytes() == b"newdata"
    assert list(tmp_path.glob("*.part")) == []
    assert progress[-1] == (7, 7)


def test_download_asset_cleans_partial_file_after_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_checker.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse([b"part"], fail_after=1),
    )

    result = update_checker.download_asset(
        "https://example.test/releases/update.zip", str(tmp_path)
    )

    assert result is None
    assert not (tmp_path / "update.zip").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_parse_release_selects_macos_arch_asset(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "darwin")
    monkeypatch.setattr(update_checker.platform, "machine", lambda: "arm64")
    data = {
        "tag_name": "v1.4.8",
        "html_url": "https://example.test/releases/v1.4.8",
        "assets": [
            {"name": "FormatMaster-1.4.8-windows-x64-portable.zip",
             "browser_download_url": "https://example.test/windows.zip"},
            {"name": "FormatMaster-1.4.8-macOS-x86_64.dmg",
             "browser_download_url": "https://example.test/intel.dmg"},
            {"name": "FormatMaster-1.4.8-macOS-arm64.dmg",
             "browser_download_url": "https://example.test/arm.dmg"},
        ],
    }

    result = update_checker._parse_release(data)

    assert result == (
        "1.4.8",
        "https://example.test/releases/v1.4.8",
        "https://example.test/arm.dmg",
    )
