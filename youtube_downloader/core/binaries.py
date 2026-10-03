from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .dependencies import locate_device_ffmpeg
from .errors import DependencyError


def application_roots() -> list[Path]:
    """Return the ordered directories that may contain bundled binaries.

    Running from source uses the repository root.  A PyInstaller one-folder
    build exposes the unpacked bundle through ``sys._MEIPASS``, and a frozen
    executable may instead (or additionally) ship ``vendor/`` beside itself, so
    the directory holding the executable is checked as well.
    """

    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root).resolve())
    roots.append(Path(__file__).resolve().parents[2])

    ordered: list[Path] = []
    for root in roots:
        if root not in ordered:
            ordered.append(root)
    return ordered


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _lookup(
    root: Path | None,
    override_name: str,
    tool: str,
    layouts: tuple[str, ...],
) -> Path | None:
    """Find a binary: an environment override, then the folders, then PATH.

    Only for binaries that either ship with the program or are simply optional.
    FFmpeg has its own lookup below because it has a fourth step - the device -
    and because candidates found there have to be run before they are believed.
    """

    override = os.environ.get(override_name)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for base in ([root] if root is not None else application_roots()):
        found = _first_existing([base / layout for layout in layouts])
        if found:
            return found
    path_candidate = shutil.which(tool)
    return Path(path_candidate) if path_candidate else None


_FFMPEG_LAYOUTS = (
    "vendor/ffmpeg/bin/ffmpeg.exe",
    "vendor/ffmpeg/ffmpeg.exe",
    "vendor/ffmpeg.exe",
    "ffmpeg.exe",
)


def find_ffmpeg(root: Path | None = None) -> Path | None:
    """Locate FFmpeg, in a fixed order of preference.

    1. the ``YTDOWNLOADER_FFMPEG`` override;
    2. a copy bundled with the application - the pinned build, and the one a
       full build tested against;
    3. anywhere else on this device: the machine-wide copy this application
       installed, a copy another program installed, or one on ``PATH``.  Each
       candidate there is only accepted once it has actually run, so a stale or
       unrelated ``ffmpeg.exe`` cannot pass as a dependency just by existing.

    A slim build has no step 2 at all, which is why step 3 carries it: the
    device-wide copy is how such a build ever finds FFmpeg.

    An explicit ``root`` restricts the search to that one folder plus ``PATH``.
    That is deliberate rather than a shortcut - it is what makes a packaging test
    deterministic instead of dependent on whatever happens to be installed on
    the machine running it, including a copy this program itself installed.
    """

    override = os.environ.get("YTDOWNLOADER_FFMPEG")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate

    if root is not None:
        found = _first_existing([root / layout for layout in _FFMPEG_LAYOUTS])
        if found is not None:
            return found
        on_path = shutil.which("ffmpeg")
        return Path(on_path) if on_path else None

    for base in application_roots():
        found = _first_existing([base / layout for layout in _FFMPEG_LAYOUTS])
        if found is not None:
            return found
    return locate_device_ffmpeg()


def find_icon() -> Path | None:
    """Locate the application icon.

    The icon is not a dependency, so this never raises: a machine without it
    simply gets the platform's default window icon.  ``assets\\app.ico`` is
    looked for under every application root, and a frozen build ships it beside
    the executable rather than inside the bundle, because a .ico has to stay a
    real file for the Windows shell to read it.
    """

    for base in application_roots():
        for layout in ("assets/app.ico", "app.ico"):
            candidate = base / layout
            if candidate.is_file():
                return candidate
    return None


def find_ffprobe(root: Path | None = None) -> Path | None:
    """Locate an ffprobe binary if one happens to be available.

    ffprobe is never called by this application, so nothing depends on the
    result and it is not bundled.  The lookup exists so a copy already on the
    user's PATH can still be reported by the self-check, and so yt-dlp can use
    it for optional probing when it wants to.
    """

    return _lookup(
        root,
        "YTDOWNLOADER_FFPROBE",
        "ffprobe",
        (
            "vendor/ffmpeg/bin/ffprobe.exe",
            "vendor/ffmpeg/ffprobe.exe",
            "vendor/ffprobe.exe",
            "ffprobe.exe",
        ),
    )


def require_ffmpeg(root: Path | None = None) -> Path:
    executable = find_ffmpeg(root)
    if executable is None:
        # Reached when a download needs FFmpeg and the first-run check either
        # never ran or was declined.  It points at the Help menu, which now
        # exists: "Check dependencies again" re-runs the same offer without
        # closing the program.  This message used to tell the reader to close
        # and reopen ClipDock, which was true then and is a worse answer now that
        # there is a button.
        raise DependencyError(
            "FFmpeg is required for video merging and MP3 conversion, but it is not "
            "installed on this computer. Open Help - Check dependencies again and "
            "accept the offer to download it, then try again."
        )
    return executable
