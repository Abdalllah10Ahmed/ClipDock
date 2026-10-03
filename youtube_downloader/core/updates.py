"""Asking GitHub whether a newer ClipDock exists.  It never fetches one.

There is deliberately no download path here, no installer invocation, and no
writing to disk.  That is not an omission waiting to be filled in: a program
that updates itself cannot be audited by reading the file it shipped, and this
program's whole job is running a third party's code against arbitrary input, so
"it will update itself" is not a property it should acquire quietly.  What this
module produces is a version string and a link to a web page the user can read
before deciding anything.

The check is on demand.  ClipDock does not contact GitHub when it starts,
because a network request nobody asked for is a decision the user did not make,
and because an offline machine has to launch normally rather than present a
connectivity problem as if it were an application problem.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import __version__
from .logging_setup import get_logger

# The public repository.  Kept next to the check that uses it so there is one
# place to change if the project ever moves, rather than a URL string typed into
# a dialog.
REPOSITORY = "Abdalllah10Ahmed/ClipDock"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

# GitHub rejects requests without one, and an honest User-Agent lets an operator
# see who is asking.  No telemetry of any kind is sent beyond this request.
USER_AGENT = f"ClipDock/{__version__}"

# The releases endpoint returns a few kilobytes.  The cap is here so a truncated,
# proxied, or hostile response cannot make the program allocate without bound;
# the body is not a trusted input and is parsed as such.
MAX_RESPONSE_BYTES = 256 * 1024

# Seconds to wait before giving up.  Long enough for a slow connection and a
# DNS lookup, short enough that an unreachable network does not leave a dialog
# waiting on nothing.
DEFAULT_TIMEOUT = 8.0

CURRENT = "current"
NEWER = "newer"
UNKNOWN = "unknown"

_LOGGER = get_logger()


@dataclass(frozen=True)
class UpdateCheck:
    """What one check found, phrased so it can be shown without re-deriving it."""

    status: str
    current: str
    latest: str | None
    release_url: str = RELEASES_URL
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status == NEWER

    @property
    def up_to_date(self) -> bool:
        return self.status == CURRENT


def parse_version(text: str) -> tuple[int, ...] | None:
    """Read a version into numbers, or None if it is not one.

    A plain ``1.2.3``, optionally tagged ``v1.2.3``.  Anything carrying a
    pre-release suffix is refused rather than guessed at: ``0.2.0-rc1`` is not
    simply older than ``0.2.0`` and it is not simply newer either, and offering
    someone a candidate build because the comparison was sloppy would be worse
    than saying nothing.
    """

    cleaned = text.strip()
    if cleaned[:1] in {"v", "V"}:
        cleaned = cleaned[1:]
    if not cleaned or "-" in cleaned or "+" in cleaned:
        return None
    parts = cleaned.split(".")
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers) or None


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a higher version than ``current``.

    Missing components count as zero so ``0.2`` and ``0.2.0`` are the same
    version rather than an update.  The comparison is numeric throughout: as
    text, ``0.10.0`` sorts below ``0.9.0``, which would offer a downgrade.
    """

    left = parse_version(latest)
    right = parse_version(current)
    if left is None or right is None:
        return False
    length = max(len(left), len(right))
    padded_left = left + (0,) * (length - len(left))
    padded_right = right + (0,) * (length - len(right))
    return padded_left > padded_right


def _unknown(current: str, reason: str) -> UpdateCheck:
    return UpdateCheck(status=UNKNOWN, current=current, latest=None, reason=reason)


def _read_body(response: object) -> str:
    """Read at most ``MAX_RESPONSE_BYTES`` of a response as text."""

    raw = response.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("the reply was larger than expected")
    return raw.decode("utf-8", "replace")


def check_for_updates(
    current: str = __version__,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: object | None = None,
) -> UpdateCheck:
    """Ask GitHub for the latest release and compare it with ``current``.

    Never raises.  Every failure becomes an ``UNKNOWN`` result with a reason, so
    an offline machine or a rate-limited one produces an explanation rather than
    an error dialog, and never a traceback on the way out of a dialog.
    """

    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    fetch = opener if opener is not None else urllib.request.urlopen
    try:
        with fetch(request, timeout=timeout) as response:  # type: ignore[operator]
            body = _read_body(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return _unknown(current, "there is no published release to compare with yet.")
        if error.code == 403:
            # GitHub's rate limit answers with 403, not 429.  Saying so is more
            # use than "request failed", because the fix is to wait, not to
            # check whether the network works.
            return _unknown(current, "GitHub refused the request. This is usually its rate limit, so trying again later will work.")
        _LOGGER.warning("update_check_http_failed code=%s", error.code)
        return _unknown(current, "GitHub could not be asked right now.")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        _LOGGER.info("update_check_unreachable reason=%s", type(error).__name__)
        return _unknown(current, "this computer could not reach GitHub.")
    except ValueError as error:
        _LOGGER.warning("update_check_bad_reply reason=%s", type(error).__name__)
        return _unknown(current, str(error))

    try:
        payload = json.loads(body)
        tag = payload["tag_name"]
        url = payload["html_url"]
    except (ValueError, KeyError, TypeError):
        _LOGGER.warning("update_check_unreadable_reply")
        return _unknown(current, "the reply from GitHub was not in the expected form.")

    tag = tag if isinstance(tag, str) else ""
    release_url = url if isinstance(url, str) and url.startswith("https://") else RELEASES_URL

    if not is_newer(tag, current):
        if parse_version(tag) is None:
            # A release exists but its tag is not a version this can compare
            # against.  Show what it is rather than claiming to know it is older.
            _LOGGER.info("update_check_uncomparable_tag tag=%s", tag)
            return UpdateCheck(
                status=UNKNOWN,
                current=current,
                latest=tag or None,
                release_url=release_url,
                reason=f"the latest release is tagged {tag or '(unnamed)'}, which is not a version number to compare.",
            )
        _LOGGER.info("update_check_up_to_date current=%s latest=%s", current, tag)
        return UpdateCheck(status=CURRENT, current=current, latest=tag, release_url=release_url)

    _LOGGER.info("update_check_newer current=%s latest=%s", current, tag)
    return UpdateCheck(status=NEWER, current=current, latest=tag, release_url=release_url)