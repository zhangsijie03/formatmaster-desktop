# Third-party notices

FormatMaster is distributed under the GNU Affero General Public License,
version 3 or later. Its downloadable artifacts include or depend on
third-party components with their own licenses.

## FFmpeg / FFprobe

The release workflow bundles static FFmpeg and FFprobe binaries through the
`ffmpeg-ffprobe-static` package. The binary package is distributed under
GPL-3.0-or-later and contains FFmpeg components under the licenses described by
the binary's accompanying source and license information. See the upstream
project for the exact build configuration and corresponding source:

- https://github.com/descriptinc/ffmpeg-ffprobe-static
- https://ffmpeg.org/legal.html

## yt-dlp

Release artifacts bundle the official `yt-dlp` executable. yt-dlp is released
under the Unlicense. See:

- https://github.com/yt-dlp/yt-dlp

## Python dependencies

The source distribution uses third-party Python packages locked in
`requirements-runtime.lock`. Release builds generate a platform-specific
`THIRD_PARTY_LICENSES.md` from the installed package metadata and publish an
SPDX SBOM with the downloadable artifacts.

Several bundled dependencies use strong copyleft or dual-license terms,
including PyMuPDF (AGPL-3.0 or commercial), PySide6-Fluent-Widgets (GPL-3.0),
EbookLib (AGPL-3.0-or-later), and mobi (GPL-3.0-only). Other Qt/PySide
components use LGPL/GPL alternatives. Binary redistributors must satisfy the
applicable source, notice, relinking, and attribution requirements or obtain
the necessary commercial licenses.

The generated inventory is a packaging aid, not legal advice. Upstream license
texts and package metadata remain authoritative.
