"""Generate platform-specific third-party inventory and an SPDX 2.3 SBOM."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if match:
            names.add(_canonical(match.group(1)))
    return names


def _license_label(metadata) -> str:
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    classifiers = [value.removeprefix("License :: ") for value in
                   metadata.get_all("Classifier", [])
                   if value.startswith("License :: ")]
    if classifiers:
        return "; ".join(classifiers)
    value = " ".join((metadata.get("License") or "").split())
    return value if 0 < len(value) <= 120 else "See package metadata"


def _project_url(metadata) -> str:
    for value in metadata.get_all("Project-URL", []):
        if "," in value:
            _label, url = value.split(",", 1)
            if url.strip().startswith("https://"):
                return url.strip()
    return (metadata.get("Home-page") or "").strip()


def _installed_packages(lock_path: Path) -> list[dict[str, str]]:
    locked = _locked_names(lock_path)
    packages = []
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        name = metadata.get("Name") or distribution.name
        if _canonical(name) not in locked:
            continue
        packages.append({
            "name": name,
            "version": distribution.version,
            "license": _license_label(metadata),
            "url": _project_url(metadata),
        })
    return sorted(packages, key=lambda item: item["name"].lower())


def _write_licenses(path: Path, packages: list[dict[str, str]]) -> None:
    lines = [
        "# Third-Party Python Packages",
        "",
        f"Generated for `{sys.platform}` from `requirements-runtime.lock`.",
        "License labels come from installed package metadata; consult each linked",
        "upstream project for the complete license text and notices.",
        "",
        "| Package | Version | Declared license | Upstream |",
        "| --- | --- | --- | --- |",
    ]
    for package in packages:
        url = package["url"]
        upstream = f"[link]({url})" if url.startswith("https://") else "—"
        license_label = package["license"].replace("|", "\\|")
        lines.append(
            f"| {package['name']} | {package['version']} | {license_label} | {upstream} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_sbom(path: Path, packages: list[dict[str, str]]) -> None:
    fingerprint = hashlib.sha256(json.dumps(
        packages, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    spdx_packages = []
    relationships = []
    for index, package in enumerate(packages, 1):
        package_id = f"SPDXRef-Package-{index}"
        spdx_packages.append({
            "SPDXID": package_id,
            "name": package["name"],
            "versionInfo": package["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
        })
        relationships.append({
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": package_id,
        })
    created = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"FormatMaster-{sys.platform}",
        "documentNamespace": (
            "https://github.com/zhangsijie03/formatmaster-desktop/"
            f"sbom/{fingerprint}"),
        "creationInfo": {
            "created": created,
            "creators": ["Tool: FormatMaster generate_legal_artifacts.py"],
        },
        "packages": spdx_packages,
        "relationships": relationships,
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path,
                        default=ROOT / "requirements-runtime.lock")
    parser.add_argument("--licenses", type=Path,
                        default=ROOT / "THIRD_PARTY_LICENSES.md")
    parser.add_argument("--sbom", type=Path)
    args = parser.parse_args()

    packages = _installed_packages(args.lock)
    if not packages:
        raise RuntimeError("未找到已安装的运行时依赖，无法生成合规清单")
    _write_licenses(args.licenses, packages)
    if args.sbom:
        _write_sbom(args.sbom, packages)
    print(f"已记录 {len(packages)} 个当前平台运行时依赖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
