"""The first-run dependency check, and the installer that resolves what is missing.

This exists so the application can be shared as a small folder instead of a
300 MB one.  FFmpeg is a 128 MB external binary that is only needed once a
download actually has to merge or convert media, so it is not shipped inside the
bundle; it is fetched on demand, verified against a pinned SHA-256, and then
installed for the whole machine.

The check is device-wide in both directions.  A machine that already has FFmpeg
for any reason - another program, a package manager, an earlier run of this one -
must not be asked to download it again, so the search covers the whole computer
rather than only the folder this copy happens to sit in.  And a copy that *is*
downloaded goes where the whole machine can use it, so a second user does not
repeat the work.  Both halves were requests, and both are only worth doing if
they can be trusted, which is why nothing is believed here until it has run.

What this module deliberately does not do is defer Python or Qt.  Those are not
"dependencies" in the sense of something a machine can be missing and then
acquire: the program is a frozen interpreter with Qt linked into it, and it is
the very thing doing the checking.  There is no smaller starting point to strip
them down to, so the honest floor for this application is the interpreter plus
Qt, and only the genuinely external tools are worth deferring.  Claiming
otherwise would be a number rather than a program.

Every failure path here returns a value that means "carry on" and reports what
happened through the log, because a dependency problem must never be the reason
the window fails to appear.  A user who is offline still gets a working program
that can list qualities and sizes; only merging and conversion are unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .. import __version__
from .errors import CancelledError, DependencyError
from .settings import default_settings_directory

_LOGGER = logging.getLogger(__name__)

# The pinned download.  These values are the same ones tools/fetch_dependencies.py
# uses to build the vendor folder, and that tool imports them from here rather
# than repeating them, so a build can never ship a binary the application would
# then refuse to install.
FFMPEG_VERSION = "9.0.2"
FFMPEG_BUILD = "n9.0.2-3-ga5923073bf"
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "autobuild-2026-09-24-14-14/"
    "ffmpeg-n9.0.2-3-ga5923073bf-win64-lgpl-9.0.zip"
)
FFMPEG_ARCHIVE_SHA256 = "ad4bddec89f4d5413f976ca4b4424c3aeec095afa029f739a2a03e9a360f06cd"
FFMPEG_BINARY_SHA256 = "ca2b72f7607527869f8f132c504765cfbab21ab05971006c7f5dd45b72c0127f"

# The archive carries every configuration BtbN publishes, which is why it is so
# much larger than the single binary that is actually kept.  This is the size
# the user is asked to agree to, so it is a constant rather than a guess.
FFMPEG_DOWNLOAD_BYTES = 171_546_855
FFMPEG_INSTALLED_BYTES = 128_300_000

_RUNTIME_FOLDER = "runtime"
_DATA_DIRECTORY = "ClipDock"

# The device-wide install location.  ``C:\ProgramData`` is used rather than
# ``C:\Program Files`` for one specific reason: the default ACL on the former
# grants BUILTIN\Users write access and hands CREATOR OWNER full control over
# whatever they create, so an ordinary user can install here *without* being an
# administrator and without a UAC prompt.  Program Files grants neither, so
# choosing it would mean the download this feature exists to avoid was replaced
# by an elevation prompt - a worse first-run experience than the 164 MB it was
# meant to remove.
SHARED_FOLDER = "ClipDock"

# The layouts an FFmpeg install is found in, in the order they are trusted.
# These are the conventional locations rather than a whole-disk scan: a scan of
# every drive would take minutes on a large disk, and would eventually surface
# an ffmpeg.exe from an unrelated tool that is not a working FFmpeg at all.  A
# candidate is only accepted after it has been *run*, so a stale or unrelated
# file cannot pass as a dependency just by existing.
_DEVICE_LAYOUTS = (
    "ffmpeg/bin/ffmpeg.exe",
    "ffmpeg/ffmpeg.exe",
    "ffmpeg.exe",
    "bin/ffmpeg.exe",
    "FFmpeg/bin/ffmpeg.exe",
)


def shared_directory() -> Path:
    """The device-wide folder a downloaded FFmpeg is installed into.

    One copy for the whole machine, so a second user - or a second program - does
    not download 164 MB that is already sitting on the disk.
    """

    program_data = os.environ.get("ProgramData")
    base = Path(program_data) if program_data else Path("C:/ProgramData")
    return base / SHARED_FOLDER / _RUNTIME_FOLDER


def per_user_directory() -> Path:
    """The fallback install location when the shared folder is not writable.

    Per user rather than beside the executable, because the executable is
    routinely un-writable: it may sit in Program Files, on a read-only share, or
    on the removable drive the folder was copied from.  A failed install must
    not be a permission error the user cannot act on.
    """

    return default_settings_directory() / _RUNTIME_FOLDER


def install_targets() -> tuple[Path, Path]:
    """Where an installed FFmpeg may be written, best first.

    The device-wide folder comes first because a dependency installed for the
    machine is a dependency for everybody: a second user, or a second program,
    should not have to download the same 164 MB that is already on the disk.
    The per-user folder is the fallback, for the machines where the shared one
    cannot be created.
    """

    return (shared_directory(), per_user_directory())


def installed_ffmpeg() -> Path | None:
    """The FFmpeg this application installed, wherever it happened to put it.

    Both locations are *looked in* rather than one being *chosen*, because this
    is called by a lookup, and a lookup that creates a directory as a side effect
    is a lookup that can fail on a machine it was only meant to read.  It
    returns None when neither holds a copy, which is the case that triggers the
    download.
    """

    for folder in install_targets():
        candidate = folder / "ffmpeg.exe"
        if candidate.is_file():
            return candidate
    return None


def _writable_target() -> Path:
    """The first install folder this process can actually write to.

    Writability is decided by creating a directory and a file rather than by
    reading the ACL, because what matters is whether *this* process can write
    *here* right now; an ACL read can disagree with that on a locked-down,
    redirected, or domain-managed machine.
    """

    for folder in install_targets():
        if _is_writable_directory(folder):
            return folder
    # Neither is writable, which should not be reachable but must not be an
    # unhandled crash.  The per-user folder is returned so the caller's error
    # message describes one concrete path it can act on.
    return per_user_directory()


def _is_writable_directory(path: Path) -> bool:
    """Whether a directory can be created and written to, tested for real."""

    probe: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".clipdock-write-probe"
        probe.write_bytes(b"")
        return True
    except OSError as error:
        _LOGGER.info("install_folder_unusable category=%s", type(error).__name__)
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def _device_search_roots() -> list[Path]:
    """Everywhere an already-installed FFmpeg is looked for on this machine."""

    roots: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            roots.append(candidate)

    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(variable)
        if value:
            add(Path(value))
    add(Path("C:/ffmpeg"))
    add(Path("C:/Program Files/ffmpeg"))
    # Chocolatey, Scoop and WinGet each install into their own tree, and all three
    # are common enough on a machine that already has FFmpeg to be worth naming.
    chocolatey = os.environ.get("ChocolateyInstall")
    add(Path(chocolatey) / "bin" if chocolatey else None)
    # Guarded because the per-profile lookups are a bonus, not the point.  A
    # machine that refuses to list its own user folders must still be searched
    # everywhere else; raising here would fail the whole check and the user would
    # be offered a download the machine does not need.
    try:
        profiles = _local_profiles()
    except OSError as error:
        _LOGGER.info("profile_scan_failed category=%s", type(error).__name__)
        profiles = []
    for user in profiles:
        add(user / "scoop" / "apps" / "ffmpeg" / "current" / "bin")
        add(user / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages")
    add(shared_directory())
    add(per_user_directory())
    return roots


def _local_profiles() -> list[Path]:
    """The local user profile folders, which is where package managers land.

    The current user's own profile comes first and the rest follow, because the
    machine's owner is the likeliest to have installed something themselves.
    """

    profiles: list[Path] = []
    users = Path(os.environ.get("SystemDrive", "C:") + "/Users")
    skipped = {"Public", "All Users", "Default", "Default User", "DefaultAppPool", "desktop.ini"}

    def add(candidate: Path) -> None:
        # Compared case-insensitively, because a Windows filesystem does not care
        # about the case and two spellings of one profile would be searched twice.
        if any(os.path.normcase(str(candidate)) == os.path.normcase(str(seen)) for seen in profiles):
            return
        profiles.append(candidate)

    own = Path.home()
    if own.parent == users:
        add(own)
    try:
        entries = sorted(users.iterdir())
    except OSError as error:
        _LOGGER.info("profile_scan_failed category=%s", type(error).__name__)
        return profiles
    for entry in entries:
        if not entry.is_dir() or entry.name.lower() in {name.lower() for name in skipped}:
            continue
        add(entry)
    return profiles


def ffmpeg_version(executable: Path) -> str | None:
    """Run a candidate and return its version, or None if it is not usable.

    Existence is not evidence.  A folder can hold a truncated download, a
    different program that happens to be called ffmpeg.exe, or a binary whose
    dependencies are missing - and each of those would pass a file-exists check
    and then fail at the moment a video is being merged, which is the worst
    possible time to find out.  Running it is cheap and turns a guess into a fact.
    """

    try:
        completed = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        _LOGGER.info("ffmpeg_probe_failed category=%s", type(error).__name__)
        return None
    # Exit status alone is not enough.  Some broken builds print a banner and
    # then fail on a missing DLL, and some unrelated programs exit zero for
    # arguments they do not understand, so the identification is checked too.
    if completed.returncode != 0:
        return None
    lines = (completed.stdout or "").splitlines()
    if not lines or "ffmpeg version" not in lines[0].lower():
        return None
    return lines[0].strip()


def _scan_device() -> tuple[Path, str] | None:
    """Find an FFmpeg this machine already has, and say where and what version.

    This is the check that stops 164 MB being downloaded a second time because
    the copy happened to be installed by something else.  Candidates are tried in
    a deliberate order: this application's own copies first, because they are the
    pinned build whose checksum was verified on the way in; then PATH; then the
    conventional install locations.

    Every candidate must actually *run* before it is accepted, including this
    application's own - see :func:`ffmpeg_version` for why existence is not
    treated as evidence.  A file in our own folder should be trustworthy because
    the install is atomic and checksum-verified, but "should be" is a claim about
    how it got there, and a disk that failed or a user who edited a file would
    both break it.  Running it costs one process start, once, because of the
    cache below.

    Cached because a candidate that failed to run will fail again, and this is
    reached from every media job; re-running a dozen executables per download
    would be pure cost.  :func:`forget_discovery` clears it once something has
    changed, which is what the installer does after writing a new copy.
    """

    for folder in install_targets():
        candidate = folder / "ffmpeg.exe"
        if not candidate.is_file():
            continue
        version = ffmpeg_version(candidate)
        if version is not None:
            return candidate.resolve(), version
        _LOGGER.info("installed_ffmpeg_did_not_run")

    on_path = shutil.which("ffmpeg")
    if on_path:
        version = ffmpeg_version(Path(on_path))
        if version is not None:
            return Path(on_path).resolve(), version
        _LOGGER.info("ffmpeg_on_path_did_not_run")

    for root in _device_search_roots():
        for layout in _DEVICE_LAYOUTS:
            candidate = root / layout
            if not candidate.is_file():
                continue
            version = ffmpeg_version(candidate)
            if version is None:
                continue
            _LOGGER.info("ffmpeg_discovered_on_device")
            return candidate.resolve(), version
        # WinGet nests each package under a package folder, a version folder and an
        # architecture folder before the binary, none of which the fixed layouts
        # above can name, so the tree is walked instead - to a bound of four,
        # which is the deepest a WinGet package reaches.  Beyond that this would
        # be a disk scan, which is what the fixed layouts are avoiding.
        if root.name == "Packages":
            for match in _walk_for_ffmpeg(root, depth=4):
                version = ffmpeg_version(match)
                if version is not None:
                    return match.resolve(), version
    return None


# The cache is bound to a name of its own rather than to ``discover_ffmpeg``, so
# that clearing it cannot be defeated by anything that has replaced that name -
# a replacement carrying the *old* cache would otherwise be cleared correctly
# while the live one stayed stale.
_CACHED_SCAN = lru_cache(maxsize=1)(_scan_device)


def forget_discovery() -> None:
    """Drop the cached device scan, because the answer may have changed."""

    _CACHED_SCAN.cache_clear()


discover_ffmpeg = _CACHED_SCAN


def locate_device_ffmpeg() -> Path | None:
    """An FFmpeg anywhere on this device, or None.  Resolved for comparison."""

    found = discover_ffmpeg()
    return found[0] if found is not None else None


def _walk_for_ffmpeg(root: Path, depth: int) -> list[Path]:
    """Find ffmpeg.exe under a package-manager tree, to a bounded depth."""

    found: list[Path] = []
    if depth <= 0:
        return found
    try:
        entries = list(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        candidate = entry / "ffmpeg.exe"
        if candidate.is_file():
            found.append(candidate)
        found.extend(_walk_for_ffmpeg(entry, depth - 1))
        if len(found) >= 4:
            break
    return found


@dataclass(frozen=True)
class DependencyReport:
    """One dependency's state, as the first-run check found it."""

    name: str
    present: bool
    path: Path | None
    needed_for: str
    download_bytes: int = 0
    installable: bool = True

    @property
    def megabytes(self) -> float:
        return self.download_bytes / 1_048_576

    @property
    def fixable(self) -> bool:
        """Whether the offer of a download would actually fix this one.

        The dialog offers one download.  A dependency that ships inside the
        program cannot be downloaded onto a machine that is already missing it,
        so reporting one as missing would put a button on screen promising
        something the click cannot deliver.
        """

        return self.installable

    @property
    def located_at(self) -> str:
        """Where it was found, in words a user can check for themselves.

        Shown only when nothing needed downloading.  Naming the folder is the
        difference between "ClipDock checked your computer and found what it
        needed" and a silent claim that might not be true, and it lets the user
        see for themselves that an unrelated installation is the one in use.
        """

        return str(self.path) if self.path is not None else ""


def check_dependencies(find_ffmpeg: Callable[[], Path | None]) -> list[DependencyReport]:
    """Report every external dependency and whether it is ready.

    ``find_ffmpeg`` is injected rather than imported so this module does not
    depend on the discovery order, and so a test can state exactly what is and
    is not on the machine instead of inheriting the machine running it.

    Both entries are reported, including the one that can never be missing, on
    purpose.  The user asked to be told what is already there, not only what is
    absent; answering "nothing to download" is only reassuring if it is
    accompanied by what was actually looked for.
    """

    ffmpeg = find_ffmpeg()
    return [
        DependencyReport(
            name="FFmpeg",
            present=ffmpeg is not None,
            path=ffmpeg,
            needed_for="merging video and audio, MP4 conversion, and MP3 encoding",
            download_bytes=FFMPEG_DOWNLOAD_BYTES if ffmpeg is None else 0,
        ),
        DependencyReport(
            name="yt-dlp",
            present=_ytdlp_version() is not None,
            path=None,
            needed_for="reading video details and downloading",
            download_bytes=0,
            installable=False,
        ),
    ]


def _ytdlp_version() -> str | None:
    """The pinned yt-dlp's version, or None if it is somehow not importable.

    Read from the installed metadata rather than by importing the package, which
    is measurably slower and would be pointless: this is a presence check, not a
    request for the module.  yt-dlp ships *inside* the program and is reported
    for completeness - it is the one dependency a user is never asked to
    download, because there is nothing on their machine they could supply it
    from.
    """

    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("yt-dlp")
    except (ImportError, PackageNotFoundError) as error:
        _LOGGER.info("ytdlp_missing category=%s", type(error).__name__)
        return None


def ytdlp_version() -> str | None:
    """The version of the yt-dlp that shipped with this build.

    Public because it is the one thing worth having in a bug report: when a
    video stops working because an extractor changed, whether the bundled
    yt-dlp is out of date is the first question, and the answer cannot be
    guessed from the application's own version.
    """

    return _ytdlp_version()


def missing_dependencies(find_ffmpeg: Callable[[], Path | None]) -> list[DependencyReport]:
    """Only what a download could actually supply.

    A dependency that is absent but cannot be installed is not something the
    dialog can act on, so including it would mean the window opens, names the
    problem, and offers a button that would not fix it.  Such a case is logged
    instead of offered.
    """

    reports = check_dependencies(find_ffmpeg)
    unfixable = [report.name for report in reports if not report.present and not report.fixable]
    if unfixable:
        _LOGGER.error("dependency_not_installable names=%s", ",".join(unfixable))
    return [report for report in reports if not report.present and report.fixable]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report(progress: Callable[[object], None], downloaded: int, total: int, note: str) -> None:
    """Send one progress event in the shape the GUI already knows how to draw."""

    percent = int(downloaded * 100 / total) if total else 0
    progress({"kind": "dependency_progress", "note": note, "done": downloaded, "total": total, "percent": percent})


def _download_archive(
    destination: Path,
    progress: Callable[[object], None],
    is_cancelled: Callable[[], bool],
) -> None:
    request = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": f"ClipDock/{__version__}"})
    try:
        connection = urllib.request.urlopen(request, timeout=60)
    except (urllib.error.URLError, OSError) as error:
        # Recorded by category and not by message: the message can contain the
        # URL, and records are not allowed to contain local paths or addresses.
        _LOGGER.warning("dependency_download_failed category=%s", type(error).__name__)
        raise DependencyError(
            "FFmpeg could not be downloaded. Check the internet connection and try again."
        ) from error

    written = 0
    with connection:
        total = int(connection.headers.get("Content-Length") or FFMPEG_DOWNLOAD_BYTES)
        with destination.open("wb") as stream:
            while True:
                if is_cancelled():
                    raise CancelledError()
                chunk = connection.read(256 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                written += len(chunk)
                _report(progress, written, total, "Downloading FFmpeg")
    _report(progress, written, total, "Downloading FFmpeg")


def _verify_archive(archive: Path) -> None:
    actual = _sha256(archive)
    if actual.lower() != FFMPEG_ARCHIVE_SHA256.lower():
        _LOGGER.warning("dependency_checksum_failed stage=archive")
        raise DependencyError(
            "The downloaded FFmpeg archive did not match its expected checksum, so it was discarded. Try again."
        )


def _extract_ffmpeg(archive: Path, destination: Path, progress: Callable[[object], None], is_cancelled: Callable[[], bool]) -> None:
    """Take ffmpeg.exe out of the archive and verify the binary on its own.

    The binary is checked separately from the archive because the two failures
    mean different things: a bad archive is a truncated or substituted download,
    while a good archive yielding a bad binary means the pin itself is wrong.
    Collapsing them into one message would send the user looking in the wrong
    place.
    """

    _report(progress, 0, 1, "Unpacking FFmpeg")
    try:
        with zipfile.ZipFile(archive) as package:
            members = [
                member
                for member in package.infolist()
                if Path(member.filename).name == "ffmpeg.exe"
            ]
            if not members:
                _LOGGER.warning("dependency_archive_incomplete")
                raise DependencyError(
                    "The downloaded FFmpeg archive did not contain ffmpeg.exe, so it was discarded. Try again."
                )
            if is_cancelled():
                raise CancelledError()
            payload = package.read(members[0])
    except zipfile.BadZipFile as error:
        _LOGGER.warning("dependency_archive_corrupt category=%s", type(error).__name__)
        raise DependencyError(
            "The downloaded FFmpeg archive was damaged and was discarded. Check the connection and try again."
        ) from error

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".exe.part")
    try:
        temporary.write_bytes(payload)
        actual = _sha256(temporary)
        if actual.lower() != FFMPEG_BINARY_SHA256.lower():
            _LOGGER.warning("dependency_checksum_failed stage=binary")
            raise DependencyError(
                "The FFmpeg binary did not match its expected checksum, so it was discarded. Try again."
            )
        # Replaced rather than written in place, so an interrupted install leaves
        # either the previous working binary or nothing, never a half-written
        # file that the next launch would treat as present and then fail to run.
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    _report(progress, 1, 1, "Unpacking FFmpeg")


def install_ffmpeg(
    progress: Callable[[object], None],
    is_cancelled: Callable[[], bool],
) -> Path:
    """Download, verify, and install FFmpeg.  Returns the installed path.

    Downloads into a temporary directory rather than next to the target, so a
    cancelled or failed run leaves no large partial file behind for the user to
    wonder about.
    """

    existing = installed_ffmpeg()
    if existing is not None:
        _LOGGER.info("dependency_already_present name=ffmpeg")
        return existing

    # Decided here, at the moment of writing, rather than assumed from the
    # lookup: only this call actually needs somewhere writable, and this is the
    # one place that can fall back without the caller having to care.
    target = _writable_target() / "ffmpeg.exe"

    with tempfile.TemporaryDirectory(prefix="clipdock-ffmpeg-") as directory:
        archive = Path(directory) / "ffmpeg.zip"
        _download_archive(archive, progress, is_cancelled)
        if is_cancelled():
            raise CancelledError()
        _verify_archive(archive)
        _extract_ffmpeg(archive, target, progress, is_cancelled)
    # The device scan is now out of date: it may have concluded that nothing was
    # installed, which is exactly the state that led to this download.
    forget_discovery()
    _LOGGER.info("dependency_installed name=ffmpeg")
    return target


def clear_cached_ffmpeg() -> bool:
    """Delete a downloaded FFmpeg.  Returns whether anything was removed."""

    target = installed_ffmpeg()
    if target is None:
        return False
    try:
        target.unlink()
    except OSError as error:
        _LOGGER.warning("dependency_remove_failed category=%s", type(error).__name__)
        return False
    forget_discovery()
    _LOGGER.info("dependency_removed name=ffmpeg")
    return True


def free_space_bytes(directory: Path) -> int | None:
    """Free space on the volume holding ``directory``, or None if unknowable."""

    try:
        usage = shutil.disk_usage(directory if directory.exists() else directory.parent)
    except OSError as error:
        _LOGGER.warning("dependency_disk_probe_failed category=%s", type(error).__name__)
        return None
    return usage.free
