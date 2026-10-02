# Changelog

All notable changes to ClipDock are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

The authoritative version lives in `youtube_downloader/__init__.py` as
`__version__`. It is mirrored in `scripts/clipdock.iss` as
`#define ProductVersion`, and a test fails if the two disagree - so the number
below is the number in the shipped program.

Release artefacts are attached to GitHub Releases rather than committed. The
installer for a release is `ClipDock-Setup.exe`; verify it against the SHA-256
printed at the end of `scripts/build_installer.ps1`.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-10-02

First public release. Everything below is new.

### Added

- **Windows installer** - `ClipDock-Setup.exe`, ~47 MB, built by
  `scripts/build_installer.ps1` with Inno Setup 6. Ordinary Next / Next /
  choose-a-folder sequence.
  - Installs **per user** to `%LOCALAPPDATA%\Programs\ClipDock` and never
    requests administrator rights, not even when launched via Run as
    administrator.
  - Uninstalls from the Start menu and from Apps & Features.
  - Uninstalling removes nothing outside the install directory: FFmpeg lives in
    `%ProgramData%` where other programs may share it, and settings live in
    `%LOCALAPPDATA%`.
  - The first wizard page explains the first-run dependency check and the
    unsigned-installer warning before anything is written to disk.
- **Device-wide FFmpeg detection** - the program searches the whole machine for
  an existing FFmpeg before offering to download one, so a copy already
  installed for any other reason is used and never re-downloaded.
- **First-run dependency check** - runs before the window appears, reports what
  it found, and asks before downloading anything. Declining is a normal answer
  and the program still opens.
- **Shared FFmpeg install** - installed once to `%ProgramData%` for the whole
  machine rather than per user or per program, so a second install of ClipDock,
  or any other tool, reuses it.
- Subtitle download in `.srt` and `.vtt`.
- Batch queue with per-item stream and quality preferences.
- MP3 conversion with cover art.
- Light and dark themes, plus the choice of either.
- Open-folder and play-after-download actions.

### Notes and known gaps

- **The installer is not code-signed.** Windows SmartScreen will warn about an
  unknown publisher on a machine that has not seen the program before. This is
  expected, and the installer explains it on its first page. A signing
  certificate was deliberately not purchased.
- **Some videos will not download.** YouTube presents a JavaScript challenge for
  certain requests, and solving it needs a JavaScript runtime (Deno or Node)
  that is not bundled - it would cost far more than it is worth. The program
  detects this and says so plainly instead of failing silently. `yt_dlp_ejs`
  and `curl_cffi` are absent from the bundle by design.
- No runtime dependency beyond PySide6 and yt-dlp; both are bundled. FFmpeg is
  the only thing ever downloaded, and only after asking.

[Unreleased]: https://github.com/OWNER/ClipDock/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/ClipDock/releases/tag/v0.1.0