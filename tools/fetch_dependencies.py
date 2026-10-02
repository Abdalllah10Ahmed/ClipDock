from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Run as a script, this file's own directory is what lands on sys.path, not the
# repository root, so the shared pins below would be unimportable without this.
# The project is not installed into the virtual environment - setup.ps1 only
# installs the pinned wheels - so the root has to be added by hand.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from youtube_downloader.core.dependencies import (
    FFMPEG_ARCHIVE_SHA256,
    FFMPEG_BINARY_SHA256,
    FFMPEG_BUILD,
    FFMPEG_URL,
    FFMPEG_VERSION,
)

YTDLP_VERSION = "2026.08.19"
YTDLP_URL = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/yt-dlp.exe"
YTDLP_SHA256 = "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a"

# A dict rather than the single shared string, because this tool also pins
# ffprobe.exe.  The application never invokes ffprobe and remove_ffprobe deletes
# it, so only ffmpeg.exe is shared with the runtime installer; the ffprobe entry
# stays local to the build and has no counterpart there.
FFMPEG_BINARY_SHA256 = {
    "ffmpeg.exe": FFMPEG_BINARY_SHA256,
    "ffprobe.exe": "984516a1153b8edd1d563fee754efcce0b86b063b162099a56da68059d5c62a8",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ClipDock/dependency-setup"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        actual = sha256(temporary)
        if actual.lower() != expected_sha256.lower():
            raise RuntimeError(f"Checksum mismatch for {url}: expected {expected_sha256}, got {actual}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_ffmpeg(archive: Path, vendor: Path) -> None:
    """Extract ffmpeg.exe only.

    ffprobe.exe is deliberately not shipped: the application never calls it,
    yt-dlp treats it as optional, and the binary costs 128 MB in a build that
    is already hundreds of megabytes.  See ``remove_ffprobe``.
    """

    target = vendor / "ffmpeg" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        members = {
            Path(member.filename).name: member
            for member in package.infolist()
            if Path(member.filename).name == "ffmpeg.exe"
        }
        if "ffmpeg.exe" not in members:
            raise RuntimeError("The FFmpeg archive is missing ffmpeg.exe")
        destination = target / "ffmpeg.exe"
        destination.write_bytes(package.read(members["ffmpeg.exe"]))
        actual = sha256(destination)
        expected = FFMPEG_BINARY_SHA256["ffmpeg.exe"]
        if actual.lower() != expected.lower():
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for FFmpeg binary ffmpeg.exe: expected {expected}, got {actual}")
    (vendor / "ffmpeg" / "VERSION.txt").write_text(
        f"FFmpeg {FFMPEG_VERSION}\nBuild: {FFMPEG_BUILD}\nSource: {FFMPEG_URL}\nSHA-256: {FFMPEG_ARCHIVE_SHA256}\n"
        "Only ffmpeg.exe is bundled. ffprobe.exe is not required by this application.\n",
        encoding="utf-8",
    )


def remove_ffprobe(vendor: Path) -> bool:
    """Delete a previously installed ffprobe.exe. Returns True when removed."""

    ffprobe = vendor / "ffmpeg" / "bin" / "ffprobe.exe"
    if ffprobe.is_file():
        ffprobe.unlink()
        return True
    return False


def write_manifest(vendor: Path) -> None:
    (vendor / "DEPENDENCIES.txt").write_text(
        "Bundled Windows dependencies\n"
        f"yt-dlp {YTDLP_VERSION}\n"
        f"yt-dlp SHA-256 {YTDLP_SHA256}\n"
        f"FFmpeg {FFMPEG_VERSION}\n"
        f"FFmpeg build: {FFMPEG_BUILD}\n"
        f"FFmpeg SHA-256 {FFMPEG_ARCHIVE_SHA256}\n"
        "FFmpeg binary SHA-256: ffmpeg.exe " + FFMPEG_BINARY_SHA256["ffmpeg.exe"] + "\n"
        "ffprobe.exe is intentionally not bundled; this application never invokes it.\n"
        "The Python application uses the pinned yt-dlp wheel; the standalone executable is included for distribution and diagnostics.\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and verify pinned Windows dependencies.")
    parser.add_argument("--vendor", type=Path, default=Path("vendor"), help="vendor directory")
    parser.add_argument("--force", action="store_true", help="download even if files already exist")
    args = parser.parse_args()
    vendor = args.vendor.resolve()
    vendor.mkdir(parents=True, exist_ok=True)

    ytdlp = vendor / "yt-dlp.exe"
    if args.force or not ytdlp.is_file() or sha256(ytdlp).lower() != YTDLP_SHA256:
        download_verified(YTDLP_URL, ytdlp, YTDLP_SHA256)
        print(f"Installed yt-dlp {YTDLP_VERSION}")
    else:
        print(f"Verified yt-dlp {YTDLP_VERSION}")

    ffmpeg = vendor / "ffmpeg" / "bin" / "ffmpeg.exe"
    needs_ffmpeg = (
        args.force
        or not ffmpeg.is_file()
        or sha256(ffmpeg).lower() != FFMPEG_BINARY_SHA256["ffmpeg.exe"]
    )
    if needs_ffmpeg:
        with tempfile.TemporaryDirectory(prefix="ffmpeg-") as temporary_directory:
            archive = Path(temporary_directory) / "ffmpeg.zip"
            download_verified(FFMPEG_URL, archive, FFMPEG_ARCHIVE_SHA256)
            install_ffmpeg(archive, vendor)
        print(f"Installed FFmpeg {FFMPEG_VERSION}")
    else:
        print(f"Verified FFmpeg {FFMPEG_VERSION}")
    if remove_ffprobe(vendor):
        print("Removed ffprobe.exe (not used by this application)")
    write_manifest(vendor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
