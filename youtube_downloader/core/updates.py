"""Asking GitHub whether a newer ClipDock exists, and fetching it when asked.

The check is a read-only question: one request to the releases endpoint, a
version comparison, and a description of what answered.  It never downloads
anything by itself, and the automatic half is still silent when there is nothing
to report.

Downloading is a separate, explicit decision.  This module can fetch the one
installer asset the project publishes, verify it, and start it - but only when a
person chose *Download* in a dialog.  The rules that make that safe are here
rather than in the window, so they hold for every caller:

* The address must be the project's own release download path, over https.  A
  redirect is followed only to a GitHub host, and the final address is checked
  again before a byte is written.
* The file is written to a temporary name and renamed only once its length (and
  its SHA-256, when the release publishes one) matches what GitHub said.  A
  truncated or tampered file is refused rather than run.
* The size has a ceiling, and the body is streamed in chunks, so a hostile
  response cannot fill the disk or the process's memory.
* Cancellation deletes the partial file.  Nothing is installed behind the
  person's back: the window closes, and the installer starts - visibly.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from .errors import CancelledError, DownloadFailure
from .logging_setup import get_logger
from .models import ProgressEvent

# The public repository.  Kept next to the check that uses it so there is one
# place to change if the project ever moves, rather than a URL string typed into
# a dialog.
REPOSITORY = "Abdalllah10Ahmed/ClipDock"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

# The one file this program is willing to download and run.  Named here rather
# than taken from the reply so a release cannot rename it into something else;
# the asset is found by this name and the file on disk is given this name.
INSTALLER_ASSET_NAME = "ClipDock-Setup.exe"
RELEASE_DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"

# GitHub rejects requests without one, and an honest User-Agent lets an operator
# see who is asking.  No telemetry of any kind is sent beyond this request.
USER_AGENT = f"ClipDock/{__version__}"

# The releases endpoint returns a few kilobytes.  The cap is here so a truncated,
# proxied, or hostile response cannot make the program allocate without bound;
# the body is not a trusted input and is parsed as such.
MAX_RESPONSE_BYTES = 256 * 1024

# The installer is around 50 MB today.  A ceiling well above that leaves room for
# growth without letting a wrong address stream until the disk is full.
MAX_INSTALLER_BYTES = 256 * 1024 * 1024
DOWNLOAD_CHUNK = 256 * 1024

# Seconds to wait before giving up.  Long enough for a slow connection and a
# DNS lookup, short enough that an unreachable network does not leave a dialog
# waiting on nothing.  A download gets longer: it is many times the bytes.
DEFAULT_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 30.0

# The hosts a redirect may land on.  GitHub serves release assets from
# objects.githubusercontent.com; the first request is always github.com.
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}
)

_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$", re.IGNORECASE)

CURRENT = "current"
NEWER = "newer"
UNKNOWN = "unknown"

_LOGGER = get_logger()


@dataclass(frozen=True)
class UpdateCheck:
    """What one check found, phrased so it can be shown without re-deriving it.

    ``installer_url`` is empty when the release does not publish the expected
    asset; the dialog then falls back to the release page rather than offering a
    download that cannot work.  The fields describe the asset, but no download
    starts unless ``download_update`` is called with this result.
    """

    status: str
    current: str
    latest: str | None
    release_url: str = RELEASES_URL
    reason: str = ""
    installer_url: str = ""
    installer_size: int = 0
    installer_digest: str = ""

    @property
    def available(self) -> bool:
        return self.status == NEWER

    @property
    def up_to_date(self) -> bool:
        return self.status == CURRENT

    @property
    def can_install(self) -> bool:
        return bool(self.installer_url)


@dataclass(frozen=True)
class DownloadedUpdate:
    """A verified installer on disk, ready to be started."""

    path: Path
    version: str = ""


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


def _installer_from_release(payload: Any) -> tuple[str, int, str]:
    """The installer asset in a release reply as ``(url, size, digest)``.

    Only a URL on the project's own release download path is accepted.  The
    reply is remote input; the ``browser_download_url`` in it is where an update
    flows from, so it is checked here rather than trusted and checked later.
    """

    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        return "", 0, ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "").lower() != INSTALLER_ASSET_NAME.lower():
            continue
        url = str(asset.get("browser_download_url") or "")
        if not url.startswith(RELEASE_DOWNLOAD_PREFIX):
            _LOGGER.warning("update_asset_url_refused")
            return "", 0, ""
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size < 0 or size > MAX_INSTALLER_BYTES:
            _LOGGER.warning("update_asset_size_refused size=%s", size)
            return "", 0, ""
        digest = str(asset.get("digest") or "")
        match = _SHA256.match(digest)
        return url, size, f"sha256:{match.group(1).lower()}" if match else ""
    return "", 0, ""


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
    installer_url, installer_size, installer_digest = _installer_from_release(payload)

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
                installer_url=installer_url,
                installer_size=installer_size,
                installer_digest=installer_digest,
            )
        _LOGGER.info("update_check_up_to_date current=%s latest=%s", current, tag)
        return UpdateCheck(
            status=CURRENT,
            current=current,
            latest=tag,
            release_url=release_url,
            installer_url=installer_url,
            installer_size=installer_size,
            installer_digest=installer_digest,
        )

    _LOGGER.info(
        "update_check_newer current=%s latest=%s installer=%s",
        current,
        tag,
        "yes" if installer_url else "no",
    )
    return UpdateCheck(
        status=NEWER,
        current=current,
        latest=tag,
        release_url=release_url,
        installer_url=installer_url,
        installer_size=installer_size,
        installer_digest=installer_digest,
    )


# --------------------------------------------------------------------- download


def _format_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_speed(bytes_per_second: float | None) -> str | None:
    if not bytes_per_second or bytes_per_second <= 0:
        return None
    return f"{_format_bytes(bytes_per_second)}/s"


def _format_eta(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total = int(seconds)
    if total < 60:
        return f"{total}s left"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s left"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m left"


def _trusted_final_url(url: str) -> bool:
    """Whether a redirect landed somewhere the installer may come from."""

    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_DOWNLOAD_HOSTS


def _download_failure(error: BaseException) -> DownloadFailure:
    """Turn a transport failure into a sentence that names what to do."""

    if isinstance(error, urllib.error.HTTPError):
        if error.code == 429:
            return DownloadFailure(
                "GitHub is rate-limiting downloads right now. Try again in a few minutes.",
                code="rate_limited",
            )
        if error.code in (401, 403):
            return DownloadFailure(
                "GitHub refused the download. The release asset may not be public.",
                code="download_failed",
            )
        if error.code == 404:
            return DownloadFailure(
                "The installer for this release is no longer on GitHub.",
                code="download_failed",
            )
        if error.code >= 500:
            return DownloadFailure(
                "GitHub could not send the update right now. Try again shortly.",
                code="server_error",
            )
        return DownloadFailure(
            f"GitHub answered the download with HTTP {error.code}.",
            code="download_failed",
        )
    if isinstance(error, TimeoutError):
        return DownloadFailure(
            "The update download timed out. Check the connection and try again.",
            code="timeout",
        )
    if isinstance(error, OSError):
        return DownloadFailure(
            "The update download was interrupted by a network problem. Try again.",
            code="network_failure",
        )
    return DownloadFailure("The update could not be downloaded.", code="download_failed")


def download_update(
    check: UpdateCheck,
    destination_dir: Path,
    *,
    progress: Callable[[ProgressEvent], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    opener: object | None = None,
) -> DownloadedUpdate:
    """Fetch the installer for ``check`` into ``destination_dir``.

    The file is written as a ``.part`` and only becomes the installer once its
    length matches the release and, when GitHub published one, its SHA-256
    matches too.  Raises ``CancelledError`` when ``cancel_check`` says stop, and
    ``DownloadFailure`` with a readable sentence for every other refusal.
    """

    if not check.can_install:
        raise DownloadFailure(
            "This release does not publish the ClipDock installer.",
            code="download_failed",
        )
    url = check.installer_url
    if not url.startswith(RELEASE_DOWNLOAD_PREFIX):
        raise DownloadFailure(
            "The update address was not the project's release download.",
            code="download_failed",
        )
    destination_dir = Path(destination_dir)
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DownloadFailure(
            "The folder for the downloaded update could not be created.",
            code="storage_error",
        ) from error

    target = destination_dir / INSTALLER_ASSET_NAME
    temporary = target.with_name(f"{target.name}.part")
    report = progress or (lambda _event: None)
    cancelled = cancel_check or (lambda: False)

    def attempt() -> tuple[int, str]:
        """One transport attempt; returns (bytes, sha256 hex)."""

        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
            method="GET",
        )
        fetch = opener if opener is not None else urllib.request.urlopen
        digest = hashlib.sha256()
        received = 0
        started = time.monotonic()
        try:
            with fetch(request, timeout=timeout) as response:  # type: ignore[operator]
                final_url = str(getattr(response, "geturl", lambda: url)())
                if not _trusted_final_url(final_url):
                    raise DownloadFailure(
                        "The update was redirected away from GitHub, so it was not kept.",
                        code="download_failed",
                    )
                headers = getattr(response, "headers", None)
                declared = 0
                if headers is not None:
                    try:
                        declared = int(headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        declared = 0
                if declared > MAX_INSTALLER_BYTES:
                    raise DownloadFailure(
                        "The update was larger than ClipDock will download.",
                        code="download_failed",
                    )
                with temporary.open("wb") as handle:
                    while True:
                        if cancelled():
                            raise CancelledError()
                        chunk = response.read(DOWNLOAD_CHUNK)  # type: ignore[attr-defined]
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > MAX_INSTALLER_BYTES:
                            raise DownloadFailure(
                                "The update was larger than ClipDock will download.",
                                code="download_failed",
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        percent = (
                            min(99.0, received / declared * 100.0) if declared else None
                        )
                        elapsed = max(0.001, time.monotonic() - started)
                        speed = received / elapsed
                        eta = (declared - received) / speed if declared and speed else None
                        report(
                            ProgressEvent(
                                phase="downloading",
                                percent=percent,
                                message="Downloading the ClipDock update",
                                speed=_format_speed(speed),
                                eta=_format_eta(eta),
                            )
                        )
        except CancelledError:
            temporary.unlink(missing_ok=True)
            raise
        except DownloadFailure:
            temporary.unlink(missing_ok=True)
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            temporary.unlink(missing_ok=True)
            _LOGGER.warning("update_download_failed category=%s", type(error).__name__)
            raise _download_failure(error) from error
        return received, digest.hexdigest()

    received, digest_hex = attempt()
    if check.installer_size and received != check.installer_size:
        temporary.unlink(missing_ok=True)
        raise DownloadFailure(
            "The update arrived incomplete. Try downloading it again.",
            code="download_failed",
        )
    if check.installer_digest:
        expected = check.installer_digest.split(":", 1)[-1].lower()
        if digest_hex != expected:
            temporary.unlink(missing_ok=True)
            _LOGGER.warning("update_digest_mismatch")
            raise DownloadFailure(
                "The update did not match the checksum GitHub published, so it was not kept.",
                code="download_failed",
            )
    try:
        temporary.replace(target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise DownloadFailure(
            "The downloaded update could not be put in place.",
            code="storage_error",
        ) from error
    _LOGGER.info("update_downloaded bytes=%s verified=%s", received, "yes" if check.installer_digest else "no")
    return DownloadedUpdate(path=target, version=str(check.latest or ""))


def launch_installer(path: Path) -> bool:
    """Start the downloaded installer, or report that it would not start.

    ``/SILENT`` shows Inno Setup's progress window but asks nothing, and
    ``/NORESTART`` keeps a finished install from rebooting the machine.  The
    caller closes ClipDock afterwards: Inno's ``CloseApplications`` handles a
    process that is still winding down, but leaving on purpose is the politer
    half of the handover.
    """

    try:
        if not path.is_file():
            return False
        subprocess.Popen(
            [str(path), "/SILENT", "/NORESTART"],
            close_fds=True,
        )
    except (OSError, ValueError):
        _LOGGER.warning("update_installer_start_failed")
        return False
    _LOGGER.info("update_installer_started")
    return True
