"""A tiny persistent settings file, so a choice survives a restart.

What it holds is a handful of preferences about how the program looks and
behaves: the theme that was picked, whether MP3s get cover art, and whether
the automatic update check is allowed plus the date it last ran.  What it
deliberately does not hold is a job.  There is no URL, no request, and no
record of anything in progress, so nothing in this file can be used to
reconstruct a download after the program is closed - which is why stopping a
download can be continued in the session it was stopped in and not after a
restart.  Keeping it that small is also why this does not need a settings
framework, a registry key, or a dependency: it is a flat JSON object of
strings in the same per-user directory the logs already use.

Everything here fails soft, and that is the whole design constraint rather than
a nicety.  This file is read during window construction, so raising from it
would turn a corrupt or read-only settings file into an application that will
not start.  A user who cannot write the file still gets a working program on
the default theme; they just do not get their choice remembered.  Every failure
path returns a value that means "carry on" and logs the outcome without the
path, because records are not allowed to contain local paths.

The file is written through a temporary file in the same directory and then
replaced, so an interrupted write leaves the previous settings intact instead
of a truncated file that reads as corrupt on the next launch.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"

# The folder this application keeps its files in under LOCALAPPDATA.  It used to
# be named after the old working title, so a folder called "YouTubeDownloader"
# may still be on disk from an earlier version; it is left alone rather than
# deleted, because removing a user's data directory is not this program's call.
_DATA_DIRECTORY = "ClipDock"


def default_settings_directory() -> Path:
    """The per-user directory for this application's own files."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _DATA_DIRECTORY
    return Path.home() / "AppData" / "Local" / _DATA_DIRECTORY


def default_settings_path() -> Path:
    return default_settings_directory() / SETTINGS_FILENAME


def read_settings(path: Path | None = None) -> dict[str, str]:
    """Return the stored settings, or an empty mapping if there are none.

    A missing file is the normal first-launch case, not an error.  Anything
    unreadable or malformed is treated the same way: the caller gets defaults
    and the program starts.
    """

    target = path or default_settings_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        _LOGGER.warning("settings_unreadable category=%s", type(error).__name__)
        return {}

    try:
        document: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _LOGGER.warning("settings_malformed action=ignored")
        return {}

    if not isinstance(document, dict):
        _LOGGER.warning("settings_malformed action=ignored")
        return {}

    # Only plain strings are kept.  A settings file is user-editable, so a
    # hand-edited value of the wrong type must not reach the rest of the app.
    return {
        str(key): value
        for key, value in document.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def write_setting(key: str, value: str, path: Path | None = None) -> bool:
    """Store one setting, keeping any others already saved.

    Returns whether the value is now on disk.  Callers are expected to ignore a
    ``False``: failing to remember a preference is not a reason to interrupt
    the user.
    """

    target = path or default_settings_path()
    settings = read_settings(target)
    if settings.get(key) == value:
        return True
    settings[key] = value

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and replace, so a crash or a full disk cannot
        # leave a half-written file that the next launch reads as corrupt.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f"{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(settings, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError) as error:
        _LOGGER.warning(
            "settings_write_failed key=%s category=%s",
            key,
            type(error).__name__,
        )
        return False
    return True
