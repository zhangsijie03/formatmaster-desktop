"""Fail fast when a source tree is unsafe or incomplete for a public release."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_GITHUB_FILE_BYTES = 100 * 1024 * 1024
REQUIRED_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "LICENSE",
    "NOTICE",
    "PRIVACY.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "THIRD_PARTY_LICENSES.md",
    "assets/icon.icns",
    "assets/icon.ico",
    "requirements.lock",
    "requirements-runtime.lock",
)
FORBIDDEN_TRACKED_PREFIXES = ("bin/", "build/", "dist/", "release/", "venv/", ".venv/")
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}


def _git(*args: str) -> bytes:
    return subprocess.check_output(("git", *args), cwd=ROOT)


def _repository_files() -> list[Path]:
    """Return tracked and non-ignored untracked files considered for a commit."""
    raw = _git("ls-files", "-z") + _git(
        "ls-files", "--others", "--exclude-standard", "-z")
    return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]


def _app_version() -> str:
    config = (ROOT / "utils/config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"$', config, re.MULTILINE)
    if not match:
        raise ValueError("utils/config.py 中未找到 APP_VERSION")
    return match.group(1)


def check(tag: str | None, require_clean: bool) -> list[str]:
    """Return release-blocking findings without changing the repository."""
    findings: list[str] = []
    repository_files = _repository_files()

    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            findings.append(f"缺少发布必需文件：{relative}")

    for path in repository_files:
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(FORBIDDEN_TRACKED_PREFIXES):
            findings.append(f"构建或本地环境文件将进入提交：{relative}")
        if path.is_file() and path.stat().st_size >= MAX_GITHUB_FILE_BYTES:
            findings.append(f"文件达到 GitHub 100 MiB 限制：{relative}")
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        content = path.read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"疑似 {label}：{relative}")

    if tag:
        normalized = tag.removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", normalized):
            findings.append(f"标签不是支持的语义版本：{tag}")
        if _app_version() != normalized:
            findings.append(f"APP_VERSION={_app_version()} 与标签 {tag} 不一致")
        notes = ROOT / "docs/releases" / f"v{normalized}.md"
        if not notes.is_file():
            findings.append(f"缺少发布说明：{notes.relative_to(ROOT)}")

    if require_clean and _git("status", "--porcelain"):
        findings.append("工作区不干净；发布必须来自完整提交")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="预期发布标签，例如 v1.5.0-beta.1")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()

    findings = check(args.tag, args.require_clean)
    if findings:
        print("发布预检失败：")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("发布预检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
