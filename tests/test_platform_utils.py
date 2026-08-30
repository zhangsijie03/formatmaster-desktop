"""跨平台系统集成工具的回归测试。"""
import os

from utils import platform_utils


def test_open_path_uses_macos_default_opener(tmp_path, monkeypatch):
    target = tmp_path / "output"
    target.mkdir()
    calls = []

    monkeypatch.setattr(platform_utils.sys, "platform", "darwin")
    monkeypatch.setattr(platform_utils.shutil, "which",
                        lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        platform_utils.subprocess, "Popen",
        lambda command: calls.append(command))

    assert platform_utils.open_path(str(target)) is True
    assert calls == [["open", os.path.abspath(str(target))]]


def test_open_path_returns_false_for_missing_target(tmp_path):
    missing = tmp_path / "missing"
    assert platform_utils.open_path(str(missing)) is False
