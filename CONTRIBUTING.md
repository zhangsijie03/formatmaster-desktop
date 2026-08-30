# Contributing to FormatMaster

By submitting a contribution, you agree that it may be distributed under the
project's AGPL-3.0-or-later license.

## Development setup

Use Python 3.11 or newer and keep the project in a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Run the same checks used by CI before opening a pull request:

```bash
python -m ruff check
python -m pytest -q
```

For a local macOS packaging smoke test:

```bash
python build.py --dmg
```

Without `--sign-identity`, the generated app receives an ad-hoc signature. It
keeps the bundle internally consistent but does not identify the developer or
bypass Gatekeeper. Do not commit generated `bin/`, `build/`, `dist/`, or
`data/` contents.

CI and release jobs install the hash-locked `requirements.lock`. When either
input requirements file changes, regenerate both locks with:

```bash
uv pip compile --universal --python-version 3.11 --generate-hashes \
  requirements.txt requirements-dev.txt -o requirements.lock
uv pip compile --universal --python-version 3.11 --generate-hashes \
  requirements.txt -o requirements-runtime.lock
```

## Release checklist

1. Update `APP_VERSION` in `utils/config.py` and add
   `docs/releases/vX.Y.Z.md`; both must match the annotated tag.
2. Run Ruff and the full test suite.
3. Run `python scripts/release_preflight.py --tag vX.Y.Z --require-clean`.
4. Push the commit and an annotated `vX.Y.Z` tag.
5. GitHub Actions reruns the release gate, then builds macOS arm64, macOS
   x86_64, and Windows x64 assets only after it passes.
6. The workflow verifies package self-tests and signatures, creates SPDX SBOMs
   and `SHA256SUMS.txt`, and publishes the GitHub Release.

## Required release signing secrets

Public releases fail closed unless these repository secrets are configured:

- `MACOS_CERTIFICATE_P12_BASE64`
- `MACOS_CERTIFICATE_PASSWORD`
- `MACOS_SIGNING_IDENTITY`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_PASSWORD`
- `WINDOWS_CERTIFICATE_PFX_BASE64`
- `WINDOWS_CERTIFICATE_PASSWORD`

The certificate must be a Developer ID Application certificate exported as a
`.p12`. The Apple app-specific password is used only by `notarytool` inside the
ephemeral GitHub runner.
