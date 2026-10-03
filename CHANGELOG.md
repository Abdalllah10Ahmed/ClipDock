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

### Fixed

- **The Help menu's mark was painted black on every dark theme.** The three dots are filled with the brush rather than stroked, and the fill colour was read from the painter's pen *after* that pen had been replaced with `NoPen` — which does not switch a pen off, it installs a new one, and a new pen's colour is black by default. The dots now take the theme's colour like the three window controls beside them.
- **The progress bar pinned itself at 100% after *Fetch details* and did not move during the download.** Two separate defects, both verified to fail without their fix. The probe's 100% was still in `_progress_value` when the download began, so the clamp that stops a retried fragment making the bar jump backward turned it into a permanent floor; the reset now happens at the start of every operation rather than being left to the previous one's completion. Separately, a superseded worker's progress events were still reaching the bar, and `JobController` now drops them.

### Added

- **A Help menu**, as a menu trigger in the drawn caption strip rather than a native menu bar, which would read as a second frame above a frameless window's caption. Its mark is three filled dots, not a character: a typed `?` is laid out and baseline-aligned by the font, so it sat at a different weight from the three window controls beside it, and a character the font happens to lack draws as a box. A gear was rejected on purpose - a gear promises a preferences page, and this menu is not one. (It also gives the button a real accessible name; a `?` was all a screen reader had to go on.) It carries *Check dependencies again*, which is now the way back to the FFmpeg offer after declining it - previously that meant closing and reopening the program - and *Remove the FFmpeg ClipDock installed*, which limits itself to ClipDock's own copy and never deletes an FFmpeg that belongs to another program.
- **An update check for ClipDock itself.** It reports whether a newer release exists and links to the releases page. It does not download or install anything, and it is on demand rather than run at launch. `core/updates.py` has no download path at all, and a test pins its only two addresses.

### Changed

- **Downloaded filenames no longer carry ` [<video id>]`** - `Alan Walker - Faded [60ItHLz5WEA].mp3` is now `Alan Walker - Faded.mp3`. The id is still added when it is needed: if the clean name is taken, the id-suffixed template is used instead, so two videos with the same title cannot silently report each other as already downloaded.

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

[Unreleased]: https://github.com/Abdalllah10Ahmed/ClipDock/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Abdalllah10Ahmed/ClipDock/releases/tag/v0.1.0