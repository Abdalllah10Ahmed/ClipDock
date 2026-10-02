"""Answer one question with evidence: can the BUNDLE do a real download?

Everything a friend runs lives in dist\\ClipDock\\_internal\\.  This imports
yt-dlp and performs a real metadata request using ONLY what is in there, with
the project's own venv and its site-packages taken off the path.  If a
third-party package is missing from the bundle, this is where it shows up -
static inspection of the folder cannot, because PyInstaller reports what it
collected and not what yt-dlp will import later at download time.

Run as:  python tools\\check_bundle.py dist\\ClipDock\\_internal
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    internal = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/ClipDock/_internal").resolve()

    # The project's own packages must not be reachable, or this proves nothing
    # about the bundle.  sys.path is rebuilt rather than appended to, and the
    # venv's site-packages is named explicitly so it can be removed.
    blocked = [p for p in sys.path if "site-packages" in p or "youtube_downloader" in p]
    sys.path = [p for p in sys.path if p not in blocked]
    sys.path.insert(0, str(internal))
    print(f"bundle        : {internal}")
    print(f"path cleared  : removed {len(blocked)} project/venv entries")
    print()

    failures: list[str] = []

    def check(label: str, fn) -> None:
        try:
            value = fn()
        except Exception as error:  # noqa: BLE001 - the point is to report anything
            print(f"  {label:<28} FAIL  {type(error).__name__}: {error}")
            failures.append(label)
        else:
            print(f"  {label:<28} ok    {value}")

    print("Imports the download path needs:")
    # yt_dlp does not expose __version__ at module level; the version lives in
    # yt_dlp.version.  Reading the wrong attribute fails the import check while
    # the import itself was fine, which is the worst kind of test failure.
    check("yt_dlp", lambda: __import__("yt_dlp.version", fromlist=["x"]).__version__)
    check("ssl (https)", lambda: __import__("ssl").OPENSSL_VERSION)
    check("urllib.request", lambda: __import__("urllib.request", fromlist=["x"]) and "loaded")
    check("socket", lambda: __import__("socket").socket.__name__)
    check("gzip", lambda: __import__("gzip").__name__)

    # Probed independently rather than as one expression, so a missing brotlicffi
    # does not stop plain brotli from being tried - and because a missing brotli
    # is worth knowing about separately: YouTube does serve brotli-encoded
    # responses, and an unhandled one is a failed download rather than a
    # slower one.
    for label, names in (
        ("brotli", ("brotli", "brotlicffi")),
        ("certifi (optional)", ("certifi",)),
    ):
        found = None
        for name in names:
            try:
                __import__(name)
            except Exception:
                continue
            found = name
            break
        note = found or "absent - yt-dlp falls back to gzip"
        print(f"  {label:<28} {'ok   ' if found else 'none'} {note}")

    print()
    print("A real HTTPS request (this is what every YouTube action starts with):")
    check(
        "GET youtube.com",
        lambda: __import__("urllib.request", fromlist=["urlopen"])
        .urlopen("https://www.youtube.com/", timeout=20)
        .status,
    )

    print()
    print("A real metadata extraction through the bundled yt-dlp:")
    try:
        from yt_dlp import YoutubeDL

        with YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
            }
        ) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=aqz-KE-bpKQ", download=False
            )
        print(f"  title      : {info.get('title')}")
        print(f"  duration   : {info.get('duration')}s")
        print(f"  formats    : {len(info.get('formats') or [])} offered")
        if not info.get("title"):
            failures.append("metadata extraction")
    except Exception as error:  # noqa: BLE001
        print(f"  metadata   FAIL  {type(error).__name__}: {error}")
        failures.append("metadata extraction")

    print()
    print("JavaScript challenge support (a known, accepted gap):")
    try:
        from yt_dlp.aes import aes_cbc_decrypt_bytes  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: F401
    except Exception:
        pass
    for name in ("yt_dlp_ejs", "yt_dlp_ejs.rijw_wrapper", "curl_cffi"):
        try:
            __import__(name)
            state = "present"
        except Exception:
            state = "ABSENT (yt-dlp falls back, or reports a JS-runtime error)"
        print(f"  {name:<28} {state}")

    print()
    if failures:
        print(f"RESULT: {len(failures)} problem(s): {', '.join(failures)}")
        return 1
    print("RESULT: the bundle can reach YouTube and extract metadata on its own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
