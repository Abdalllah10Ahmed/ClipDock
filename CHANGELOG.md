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

## [0.3.0] - 2026-10-08

Five fixes and four additions. The additions are the four things asked for after
0.2.0 - Pause, a Start-with-Windows option, an automatic update check, and a
download history - and the fixes are the four findings left open in the backlog
plus one found by testing a build the way a person would. Two of the fixes are
the same finding on two paths: YouTube refuses a small, numerous request with
429, and the code that carried it could not ask again.

### Fixed

- **The frozen executable carried no version resource.** Task Manager, file
  Properties and Windows' installed-programs list showed nothing for
  `ClipDock.exe`, while `ClipDock-Setup.exe` named a version - so the installer
  said what it installed and the program said nothing. `tools\make_version_file.py`
  now generates the resource from `__version__` and the build passes it to
  PyInstaller. It is generated into `build\` rather than committed, because a
  committed copy would be a fifth place for the version to go stale. Verified by
  reading the built **and** the installed executable: both report `FileVersion
  0.3.0`, `ProductName ClipDock`, `OriginalFilename ClipDock.exe`.
- **The "smaller file" refusal gave a reason that was false.** The filter only
  offers H.264, but the message told the user the passed-up streams were WebM
  that would need re-encoding. Measured on two videos they are MP4 containers
  (`ext=mp4`, `av01`) that need no re-encoding at all - they were refused for
  the codec alone. The refusal stands (H.264 for playback compatibility) and the
  ranking is deliberately unchanged, because the HLS variant's higher bitrate is
  real rather than an artefact; what changed is that the message now says which
  stream was passed up and what it would have saved.
- **A throttled caption request was never retried.** yt-dlp cannot retry an
  HTTP 429 on the code path that carries captions, and its `"retries"` option
  buys nothing for it, so one refused request ended the run. The application now
  recognises the rate limit and asks again itself - up to three attempts, 10
  seconds apart and then 30 - instead of handing the request back to something
  that will not retry it.
- **A throttled media or thumbnail request was never retried either.** The same
  reading explained why video downloads looked safe (a fragment failure *is*
  retried by yt-dlp) while a thumbnail was not. The application now waits 60
  seconds and then 120 before trying again, and a continuation picks up the
  part-written file rather than starting it over.
- **The progress bar did not move during a download, and pinned itself at 100%
  before the file was done.** Found by testing a built release the way a person
  would. Two defects, both measured on a real download and both regression-tested.
  *(1) Progress was only delivered when the job ended.* The worker's `progress`
  signal was connected to a plain callable, and that callable only runs after the
  job's thread returns, because the signal is emitted from yt-dlp's fragment
  threads while their parent thread is busy running the job - its event loop
  never reaches the callable until the job is over. Every event of a download
  therefore arrived in one burst at the end (measured on one run: an event took
  from 9.667 s to 20.683 s to be relayed). Fetching details looked fine because
  those events are emitted from the worker's own thread, which is also why the
  headless tests never saw it - the fake engines emit from the worker's thread.
  The `progress` signal is now connected to a small object that lives on the main
  thread, where the bar is; the guard that drops a superseded worker's events is
  unchanged. *(2) yt-dlp's first fragment estimate reported the file already
  downloaded.* Before any fragment completes, the estimate equals the bytes just
  read (measured 712 of 712 bytes), which is 100% - and the clamp that stops a
  retried fragment making the bar jump backward turned that into a permanent
  floor. An estimate that claims the whole file while the download is still
  running is now reported as *unknown* instead of as 100%.

### Added

- **Pause, and a Resume that only appears when it is real.** Pause stops the job
  and keeps everything already downloaded. It is *not* a true pause - yt-dlp has
  no pause primitive - and nothing here claims the continuation picks up at the
  exact byte, because that has not been verified against YouTube. Resume appears
  beside Retry only when a part-written file with real bytes was left by a job
  **this session** stopped, hidden otherwise rather than greyed out, and it
  re-runs the job that was paused rather than whatever is on screen now.
  Closing the program ends the offer.
- **A Start with Windows option in the installer, ticked by default.** The
  label is the consent: while it is ticked, a frameless ClipDock window opens by
  itself at every sign-in. It writes one quoted value under
  `HKCU\...\CurrentVersion\Run`, an upgrade that unticks the box retracts it,
  and uninstall removes it - all three confirmed by a real install, two upgrades
  and an uninstall run on a machine rather than by reading the script.
- **An automatic update check.** Eight seconds after the window appears, once a
  day, asking GitHub whether a newer release exists. It never downloads or
  installs anything, and it is silent when the current release is the newest, so
  on an up-to-date machine it says nothing at all and writes only the date it
  ran. *Help → Check for ClipDock updates* still asks immediately.
- **A download history**, in a window of its own beside Retry, grouped by job
  and newest first: what was requested, which folder it went to, when, and how
  it ended - files, partial, or failed and why. It keeps the request as well as
  the outcome, so a later version can rebuild a job from a record; nothing does
  that yet. Two hundred jobs are kept and *Clear history* erases them behind a
  confirmation, leaving the downloaded files themselves alone.

### Notes and known gaps

- **A paused download is a stop, not a pause**, and whether resuming continues
  at the byte reached has still not been established against the live endpoint.
  Nothing in the program claims otherwise.
- **A 25-language caption batch has never completed** against the live endpoint
  — refused on the first request in 3.9 seconds - so the pacing, the cap and the
  recovery exist because of measured throttling rather than because a full batch
  was watched succeed.
- **A history record cannot yet restart anything.** The request is stored, but
  nothing reads it back, so closing the program still ends a stopped download.

## [0.2.0] - 2026-10-03

Three fixes and three additions, all of which are about the program telling the
truth about what it is doing. The Help menu is new, so the installer now offers a
route back to the FFmpeg offer after you have declined it, and the update check
tells you when a newer ClipDock exists without ever installing one.

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

[Unreleased]: https://github.com/Abdalllah10Ahmed/ClipDock/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Abdalllah10Ahmed/ClipDock/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Abdalllah10Ahmed/ClipDock/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Abdalllah10Ahmed/ClipDock/releases/tag/v0.1.0