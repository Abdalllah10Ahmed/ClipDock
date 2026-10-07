"""What was downloaded, when, and where, so closing the window does not lose it.

Until this existed the only record of a finished job was whatever the window
happened to be showing, which made the program's memory exactly as long as the
window was open.  This is that record as a file.

It is one entry per **job**, not one line per file, because the two answer
different questions.  A 200-video playlist is one thing a person did, and 200
unrelated lines would bury every single-video download ever made underneath it.
A job therefore carries a header - what was asked for, the folder, when it ran,
how much was saved and how much was not - and its files are listed beneath it,
so the newest job answers "what did I just get" and an old one is still findable
by scrolling.

Two kinds of information are kept, and they are kept for different reasons:

* The **listing** is what the window shows: each file's title, where it went,
  its outcome, the reason when there is one, and the link it came from where a
  link can be proved.  This is the answer to "what did I get last week".
* The **request** is the set of choices the job was made with - destination,
  kind, quality, captions.  The window never shows it.  It is written because
  the obvious next question after "what did I get" is "get it again", and
  adding it later would mean either rewriting the format and losing everybody's
  existing history or writing a migration for a file nobody asked to migrate.

That second point is what makes this file different from `settings.json`, whose
docstring says it deliberately holds no job.  This one does: it is a log of what
the user asked the program to do, so it holds URLs and local paths on purpose.
The two are separate files for that reason, and neither is allowed to become the
other's backup.

Everything here fails soft, for the same reason `settings.py` does and with the
same stakes.  A history entry is written on the way out of a finished download,
so a program that refused to say "download complete" because a log file could
not be written would have its priorities backwards.  Every path therefore
returns a value meaning "carry on", and every message is logged without the
path - the record may contain where the user keeps their files, but the log
must not become a second copy of it.

Writes go through a temporary file in the same directory and are then swapped
into place, so an interrupted write leaves the previous history intact rather
than a truncated file that reads as corrupt on the next launch.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .models import (
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    PlaylistDownloadRequest,
    PlaylistDownloadResult,
    QueueDownloadRequest,
    QueueDownloadResult,
)
from .settings import default_settings_directory

_LOGGER = logging.getLogger(__name__)

HISTORY_FILENAME = "history.json"

# Oldest jobs dropped past this many.  A single playlist run is one entry
# whatever it contains, so counting jobs - not files - is what keeps this from
# being a number nobody can reason about: two hundred jobs is months of ordinary
# use for most people and a file that still opens instantly.
MAX_HISTORY_JOBS = 200

# Outcomes a *job* can have.  These are the words the window shows, so they are
# part of the file's format rather than presentation: a later version has to be
# able to read an outcome written by this one.
OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_PAUSED = "paused"
OUTCOME_CANCELLED = "cancelled"

# Outcomes an individual *file* can have.
ITEM_SAVED = "saved"
ITEM_FAILED = "failed"
ITEM_STOPPED = "stopped"

# The attributes worth keeping from a request, and nothing else.  Listed
# explicitly rather than walked with `dataclasses.fields` so that adding a
# field to a request does not silently start recording it: whether a new
# setting belongs in the history is a decision, not a side effect.
_REQUEST_SETTING_NAMES = (
    "url",
    "urls",
    "mode",
    "media",
    "audio_bitrate",
    "preference",
    "subtitle_format",
    "subtitle_source",
    "subtitle_language",
    "embed_cover",
)


def default_history_path() -> Path:
    """Where the record lives: beside the settings and the logs."""

    return default_settings_directory() / HISTORY_FILENAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _plain(value: Any) -> Any:
    """Reduce a value to something JSON will accept, without losing what it meant."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def describe_request(request: Any) -> dict[str, Any]:
    """The choices a job was made with, as plain data that outlives the objects.

    Deliberately not ``dataclasses.asdict(request)``.  That would carry the
    video's whole ``VideoInfo`` along with every quality YouTube offered for
    it - none of which is a choice anybody made - and a ``Path``, which JSON
    refuses.  What a continuation needs is the decisions: where it was going,
    what kind of thing it was, which quality, which captions.  Those are
    exactly the fields with plain values.
    """

    if request is None:
        return {}
    described: dict[str, Any] = {}
    for name in _REQUEST_SETTING_NAMES:
        if hasattr(request, name):
            described[name] = _plain(getattr(request, name))
    if hasattr(request, "output_dir"):
        described["output_dir"] = str(getattr(request, "output_dir"))
    quality = getattr(request, "quality", None)
    if quality is not None:
        # Only the identity of the choice.  Estimated sizes are a reading of
        # what YouTube offered at one moment and are meaningless later.
        described["quality"] = {
            name: getattr(quality, name)
            for name in ("format_id", "height", "fps")
            if hasattr(quality, name)
        }
    return described


def _requested_line(request: Any) -> str:
    """What the job was asked for, phrased for a header."""

    if isinstance(request, QueueDownloadRequest):
        return f"{len(request.urls)} links"
    url = getattr(request, "url", "")
    return str(url)


def _mode_label(request: Any) -> str:
    """Which of the six things a person could have asked for this was.

    Taken from the request's *type* rather than from its ``mode`` attribute
    where there is one, because ``DownloadRequest.mode`` is the kind of media
    inside a single-video job and a playlist has no ``mode`` at all - it has
    ``media``, whose value is "video" or "audio".  Reading either of those
    straight through would label a playlist of videos as a plain Video job,
    which is exactly the ambiguity the grouped view exists to avoid.
    """

    if isinstance(request, PlaylistDownloadRequest):
        return DownloadMode.PLAYLIST.value
    if isinstance(request, QueueDownloadRequest):
        return DownloadMode.QUEUE.value
    mode = getattr(request, "mode", None)
    return str(_plain(mode) or "")


def _item(
    title: str,
    destination: str,
    outcome: str,
    reason: str = "",
    url: str = "",
) -> dict[str, str]:
    return {
        "title": title,
        "destination": destination,
        "outcome": outcome,
        "reason": reason,
        "url": url,
    }


def _unique_title_index(request: Any) -> dict[str, list[Any]]:
    """Titles appearing exactly once in what a playlist was asked to fetch.

    Built so a repeated title cannot be answered with the wrong video: if two
    entries share a name, neither gets a lookup and both fall back to what is
    actually on disk.  That is the same mistake ``:330`` records elsewhere -
    repeating what the filename rules are, in order to work backwards from a
    filename, is how a clean filename stopped being recognised once already.
    """

    info = getattr(request, "info", None)
    by_title: dict[str, list[Any]] = {}
    for video in getattr(info, "videos", ()) or ():
        by_title.setdefault(video.title, []).append(video)
    return by_title


def _lookup(index: dict[str, list[Any]], title: str) -> Any | None:
    matches = index.get(title) or []
    return matches[0] if len(matches) == 1 else None


def _single_items(request: Any, result: Any, outcome: str, reason: str, folder: str) -> list[dict[str, str]]:
    """The one or more files a single-video job produced.

    A caption job writes one file per language, and ``subtitle_paths`` already
    contains every one of them; the plain ``path`` would hide all but the last.
    """

    title = str(getattr(getattr(request, "info", None), "title", "") or "")
    url = str(getattr(request, "url", ""))
    if result is None:
        # Nothing provable was written, but the job still happened: the title
        # and the folder are known, and the reason is what it stopped on.
        return [_item(title or url, folder, ITEM_STOPPED if outcome != OUTCOME_FAILED else ITEM_FAILED, reason, url)]

    paths: tuple[Path, ...] = ()
    if getattr(result, "mode", None) is DownloadMode.SUBTITLES:
        # `subtitle_paths` already includes `path`; the plain `path` alone
        # would list only the last language of a caption job.
        paths = tuple(getattr(result, "subtitle_paths", ()) or ()) or (result.path,)
    else:
        paths = (result.path,)
    warning = str(getattr(result, "warning", "") or "")
    return [
        _item(title or path.stem, str(path), ITEM_SAVED, warning, url)
        for path in paths
    ]


def _playlist_items(request: Any, result: Any, folder: str) -> list[dict[str, str]]:
    index = _unique_title_index(request)
    items: list[dict[str, str]] = []
    for path in result.paths:
        # The file's own name first: it is what is on disk, and it is the only
        # thing that can be trusted without rebuilding the output template's
        # rules.  The real title and its link are used only when exactly one
        # video in the playlist answers to that name - a repeated title would
        # otherwise be answered with the wrong video, which is the mistake
        # recorded against `:330` elsewhere in the map.
        video = _lookup(index, path.stem)
        title = video.title if video is not None else path.stem
        url = video.normalized_url if video is not None else ""
        items.append(_item(title, str(path), ITEM_SAVED, "", url))
    for title, message in result.failures:
        video = _lookup(index, title)
        items.append(
            _item(
                video.title if video is not None else title,
                folder,
                ITEM_FAILED,
                message,
                video.normalized_url if video is not None else "",
            )
        )
    return items


def _queue_items(request: Any, result: Any, folder: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in result.items:
        if entry.path is not None:
            items.append(_item(entry.title or entry.url, str(entry.path), ITEM_SAVED, "", entry.url))
        else:
            items.append(_item(entry.title or entry.url, folder, ITEM_FAILED, entry.error, entry.url))
    return items


def build_job(
    request: Any,
    result: Any = None,
    *,
    started: str = "",
    outcome: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Assemble one history entry from what a job was asked for and what it gave back.

    ``result`` is ``None`` when nothing can be listed - the job failed outright,
    or it was stopped - and the outcome is then whatever the caller says it
    was.  The whole thing is defensive on purpose: this runs on the way out of
    a finished download, and refusing to record it because an attribute was
    missing would be the wrong trade for a log.
    """

    folder = str(getattr(request, "output_dir", "") or "")
    if not isinstance(request, (DownloadRequest, PlaylistDownloadRequest, QueueDownloadRequest)):
        return {}

    items: list[dict[str, str]] = []
    attempted = 0
    if isinstance(result, PlaylistDownloadResult):
        items = _playlist_items(request, result, folder)
        attempted = int(result.total or 0)
    elif isinstance(result, QueueDownloadResult):
        items = _queue_items(request, result, folder)
        attempted = int(result.total or 0)
    elif isinstance(result, DownloadResult):
        items = _single_items(request, result, outcome or OUTCOME_COMPLETE, reason, folder)
        attempted = len(items)
    elif result is None:
        items = (
            _single_items(request, None, outcome or OUTCOME_FAILED, reason, folder)
            if isinstance(request, DownloadRequest)
            else []
        )
        attempted = len(items)
    else:
        return {}

    # Counted from the rows that are actually listed rather than taken from the
    # result's own totals.  "Saved" and "Failed" are the two things a reader can
    # check against the file names beside them, and a header that disagreed with
    # its own list would be the log arguing with itself.
    succeeded = sum(1 for item in items if item["outcome"] == ITEM_SAVED)
    failed = sum(1 for item in items if item["outcome"] == ITEM_FAILED)

    if not outcome:
        if result is None:
            # Nothing was produced and nothing said why, so it did not succeed.
            outcome = OUTCOME_FAILED
        elif failed and succeeded:
            outcome = OUTCOME_PARTIAL
        elif failed:
            outcome = OUTCOME_FAILED
        elif getattr(result, "warning", ""):
            # Files were written, but not all of what was asked for arrived.
            outcome = OUTCOME_PARTIAL
        else:
            outcome = OUTCOME_COMPLETE

    return {
        "started": started or _now(),
        "finished": _now(),
        "mode": _mode_label(request),
        "requested": _requested_line(request),
        "folder": folder,
        "outcome": outcome,
        "reason": reason,
        "succeeded": succeeded,
        "failed": failed,
        "total": max(attempted, len(items)),
        "items": items,
        # Written and never shown.  See the module docstring: this is the half
        # that makes "get it again" possible later without rewriting the file.
        "request": describe_request(request),
    }


def read_history(path: Path | None = None) -> list[dict[str, Any]]:
    """Every recorded job, newest last, or an empty list if there is nothing.

    A missing file is the normal first-launch case.  Anything unreadable or
    malformed reads as empty rather than raising, and an entry of the wrong
    shape is dropped rather than handed to a window that would then have to
    defend itself against it.
    """

    target = path or default_history_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        _LOGGER.warning("history_unreadable category=%s", type(error).__name__)
        return []

    try:
        document: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _LOGGER.warning("history_malformed action=ignored")
        return []
    if not isinstance(document, list):
        _LOGGER.warning("history_malformed action=ignored")
        return []

    jobs: list[dict[str, Any]] = []
    for entry in document:
        if not isinstance(entry, dict):
            continue
        items = entry.get("items")
        entry["items"] = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        jobs.append(entry)
    return jobs


def _write(jobs: list[dict[str, Any]], target: Path) -> bool:
    """Replace the file with ``jobs``, or report that it could not be done."""

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
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
                json.dump(jobs, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except (OSError, TypeError, ValueError) as error:
        _LOGGER.warning("history_write_failed category=%s", type(error).__name__)
        return False
    return True


def record_job(job: dict[str, Any], path: Path | None = None) -> bool:
    """Write one finished job down, dropping the oldest once the cap is passed.

    Returns whether it is now on disk.  A ``False`` is not worth telling
    anybody about: the download it describes already happened, and a log that
    could not be written must never be the reason a result goes unreported.
    """

    if not isinstance(job, dict):
        return False
    target = path or default_history_path()
    jobs = read_history(target)
    jobs.append(job)
    return _write(jobs[-MAX_HISTORY_JOBS:], target)


def clear_history(path: Path | None = None) -> bool:
    """Erase the record.  Returns whether there is now nothing in it."""

    target = path or default_history_path()
    if not target.exists():
        return True
    return _write([], target)
