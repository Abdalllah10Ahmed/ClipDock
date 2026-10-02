# Third-party notices

This project is intended for Windows 10/11 and uses the following third-party components. The complete license texts are distributed with the corresponding packages/builds and should accompany any redistributed application bundle.

## PySide6 / Qt for Python

- Version: 6.11.2
- Project: https://www.pyside.org/
- License: LGPL-3.0-only / GPL-3.0-only compatibility terms apply as described by the Qt for Python distribution.

## yt-dlp

- Version: 2026.08.19
- Project: https://github.com/yt-dlp/yt-dlp
- License: Unlicense (the Python package); the standalone executable includes its own third-party notices.

## FFmpeg

- Version: 9.0.2
- Windows build: BtbN FFmpeg-Builds `n9.0.2-3-ga5923073bf`, win64 LGPL 9.0 archive
- Project: https://ffmpeg.org/ and https://github.com/BtbN/FFmpeg-Builds
- License: the build is distributed under the LGPL configuration recorded in `vendor/ffmpeg/VERSION.txt` and the upstream FFmpeg license notices.

The dependency fetch script verifies the archive SHA-256 and the extracted `ffmpeg.exe` SHA-256 value before installation. `ffprobe.exe` is not extracted and is not redistributed; the application never invokes it. Do not remove the accompanying license and source-information notices when redistributing a bundle.

This list is complete for the current build. The subtitle, batch-queue, stream-preference, open-folder/play, theme, saved-theme, and cover-art features add no third-party component: captions are fetched by the already-pinned yt-dlp and written directly as `.srt`/`.vtt` with no converter, the queue and the preference logic are ordinary application code, opening folders and playing files use the standard Windows shell through Qt, the themes are stylesheet values defined in this repository, the theme preference is a JSON file written with the standard library, and cover art is fetched by yt-dlp and attached to the MP3 by the already-bundled FFmpeg using the postprocessor yt-dlp already ships.

The two see-through themes once shipped - Glass and Transparent - called the Windows compositor over `ctypes`. Both have been removed, along with that module, because neither read well enough to keep. Nothing else in the application uses `ctypes`; the remaining `ctypes` import in the window is for the `WM_NCHITTEST` hit-test reply, which calls the window manager on the operating system already required to run the application. No Windows code is copied, linked, or redistributed.

## Content use

The application does not grant permission to copy third-party media. Users are responsible for having the necessary rights and for following applicable law and platform terms.
