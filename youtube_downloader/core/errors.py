from __future__ import annotations

import errno
import re
from typing import Any


class AppError(Exception):
    """An expected application error with a safe user-facing explanation."""

    def __init__(self, message: str, *, code: str = "application_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CancelledError(AppError):
    def __init__(self) -> None:
        super().__init__("The operation was cancelled.", code="cancelled")


class DependencyError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="missing_dependency")


class ExtractionError(AppError):
    def __init__(self, message: str, *, code: str = "extraction_failed") -> None:
        super().__init__(message, code=code)


class UnsupportedContentError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="unsupported_content")


class DownloadFailure(AppError):
    def __init__(self, message: str, *, code: str = "download_failed") -> None:
        super().__init__(message, code=code)


class StorageError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="storage_error")


def _exception_text(error: BaseException) -> str:
    return " ".join(str(error).lower().split())


# Ordered: the first vocabulary whose tokens appear in the text wins, so the
# narrow ones come before the broad ones.  This is a *diagnosis*, not a
# message parser -- the tokens are the phrases that identify a cause with no
# ambiguity, because the cost of guessing wrong here is telling the user to
# check their connection when the service is refusing them.
_YTDLP_REASON_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rate_limited", ("too many requests", "http error 429", "429 client error", "http 429", "rate limit")),
    ("private", ("private video", "this video is private", "sign in if you", "members-only")),
    ("unavailable", ("video unavailable", "has been removed", "no longer available", "does not exist", "not available in your country")),
    ("age_restricted", ("age-restricted", "age restricted", "confirm your age")),
    ("live_event", ("live stream", "live event", "is a premiere", "premieres in")),
    ("verification_required", ("sign in to confirm", "login required", "sign in required", "confirm you are not a bot", "confirm you're not a bot")),
    ("cookies_required", ("use --cookies", "cookies from browser", "account cookies")),
    ("format_unavailable", ("requested format is not available", "no video formats", "no formats found")),
    ("extractor_failure", ("unable to download webpage", "failed to parse", "unsupported url", "no suitable extractor")),
    ("network_failure", ("timed out", "timeout", "connection reset", "connection refused", "temporary failure in name resolution", "network is unreachable", "getaddrinfo", "ssl", "remote end closed", "service unavailable", "http error 5", "http error 503")),
)


def classify_yt_dlp_message(message: str) -> str:
    """Reduce a yt-dlp message to one fixed token, or ``"unrecognised"``.

    yt-dlp's own messages carry video URLs, titles and paths, which is why
    :class:`~.logging_setup.SafeYtDlpLogger` never writes them out.  Discarding
    the text alone leaves a log where a rate limit, a dropped connection and a
    removed video all read ``yt_dlp_error``, which is the one diagnostic a
    throttle needs.  Reducing to a token keeps the *kind* of failure while
    dropping everything identifiable, so the vocabulary has to be a closed set
    and every token has to be a phrase that identifies the cause on its own.
    """
    text = " ".join(message.lower().split())
    for reason, tokens in _YTDLP_REASON_TOKENS:
        if any(token in text for token in tokens):
            return reason
    return "unrecognised"


def rate_limited_failure() -> DownloadFailure:
    """The one rate-limit explanation, built once so it cannot drift apart.

    A caller that catches an HTTP error itself -- the thumbnail fetch does, and
    has to, because it must not let an ``OSError`` subclass fall through to the
    "the file could not be written" branch -- would otherwise have to restate this
    to avoid reporting a 429 as a broken connection.  Two copies of this string
    would be the same defect as the one it fixes.
    """

    return DownloadFailure(
        "YouTube refused the request because too many were sent too quickly (HTTP 429). "
        "That is a limit on the service rather than a problem with your connection; "
        "wait a few minutes and try again. A long caption batch is the usual cause.",
        code="rate_limited",
    )


def friendly_error(error: BaseException) -> AppError:
    if isinstance(error, AppError):
        return error
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return CancelledError()

    text = f"{_exception_text(error)} {error.__class__.__name__.lower()}"
    if re.search(r"\b(?:cancelled|canceled)\b", text):
        return CancelledError()
    # A 429 is a decision the service made about how fast it is being asked,
    # not a fault in the person's connection, so it is named before the network
    # branch below rather than being counted with it.
    if classify_yt_dlp_message(text) == "rate_limited":
        return rate_limited_failure()
    if "no space left" in text or "disk full" in text or "not enough space" in text:
        return StorageError("The drive is full. Free some space and try again.")
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return StorageError("The drive is full. Free some space and try again.")
    if "ffmpeg" in text and ("not found" in text or "not installed" in text or "unable to locate" in text):
        return DependencyError("FFmpeg is required for this operation but could not be found. Run the dependency setup and try again.")
    if "ffmpeg" in text and ("postprocessing" in text or "convert" in text or "merge" in text):
        return DownloadFailure("FFmpeg could not convert or merge the media into the requested format.", code="ffmpeg_failed")
    if "private video" in text or "this video is private" in text:
        return UnsupportedContentError("This video is private, so it cannot be downloaded without access.")
    if "this video has been deleted" in text or "video deleted" in text:
        return UnsupportedContentError("This video has been deleted by the uploader.")
    if "age-restricted" in text or "age restricted" in text or "confirm your age" in text:
        return UnsupportedContentError("This video is age-restricted. This version does not sign in or bypass age verification.")
    if "this video requires a premium subscription" in text or "members only" in text or "premium" in text:
        return UnsupportedContentError("This video is for premium subscribers only and cannot be downloaded without a subscription.")
    if ("not available in your country" in text or "geo restricted" in text or "geo-restricted" in text or ("region" in text and "available" in text)):
        return UnsupportedContentError("This video is not available from the current region.")
    if "video unavailable" in text or "has been removed" in text or "no longer available" in text or "does not exist" in text:
        return UnsupportedContentError("This video is unavailable, removed, or restricted by YouTube.")
    if "live stream" in text or "live event" in text or "premiere" in text:
        return UnsupportedContentError("Live streams and premieres cannot be downloaded with this version.")
    if "copyright" in text or "dmca" in text or "copyrighted" in text:
        return UnsupportedContentError("This video is blocked due to a copyright claim.")
    if "inappropriate" in text or "community guidelines" in text:
        return UnsupportedContentError("This video was removed for violating YouTube's community guidelines.")
    if "drm" in text or ("protected" in text and "content" in text):
        return UnsupportedContentError("This video is protected and cannot be downloaded by this application.")
    if any(token in text for token in ("js challenge", "javascript challenge", "challenge solver", "challengeprovider", "jscallengeproviderrejectedrequest", "yt-dlp-ejs", "yt_dlp_ejs", "remote component")):
        return DependencyError(
            "YouTube requires JavaScript challenge support for this video. The approved dependency set does not include yt-dlp-ejs or a bundled JavaScript runtime, so this video cannot be downloaded."
        )
    if "sign in to confirm" in text or ("bot" in text and "confirm" in text) or "login required" in text or "sign in required" in text:
        return DownloadFailure("YouTube asked for additional verification. This version does not use accounts or bypass that check.", code="verification_required")
    if "cookie" in text or "cookies" in text:
        return DependencyError("YouTube requires cookies for this video. This version does not store or use account cookies.", code="cookies_required")
    if "requested format is not available" in text or "no video formats" in text or "no formats" in text:
        return DownloadFailure("The selected quality is no longer available. Fetch the video details again and choose another option.", code="format_changed")
    if "unsupported url" in text or "no suitable extractor" in text:
        return ExtractionError("This link is not a supported YouTube video link.", code="unsupported_url")
    # The rate limit is deliberately absent here: it is classified above, so a
    # 429 can no longer be reported as a broken connection.
    if any(token in text for token in ("timed out", "timeout", "connection reset", "connection refused", "temporary failure", "network is unreachable", "getaddrinfo", "ssl", "remote end closed", "service unavailable", "http error 5", "http error 503")):
        return DownloadFailure("The network connection was interrupted or unavailable. Check the connection and try again; partial downloads may resume.", code="network_failure")
    if "unable to download webpage" in text or "failed to parse" in text or ("extract" in text and "failed" in text):
        return ExtractionError("YouTube could not be queried. The site or extractor may have changed; update the bundled yt-dlp dependency and try again.", code="extractor_changed")

    if isinstance(error, TimeoutError):
        return DownloadFailure("The operation timed out. Check the network connection and try again.", code="network_failure")
    if isinstance(error, OSError):
        return StorageError("The file could not be written. Check the destination folder and available disk space.")
    if error.__class__.__name__ in {"DownloadError", "ExtractorError", "YoutubeDLError"}:
        return DownloadFailure("The download could not be completed because YouTube returned an error. Fetch the details again or try a different quality.", code="download_failed")
    return DownloadFailure("An unexpected error stopped the operation. Try again; if it persists, check the application log.", code="unexpected")


def safe_log_error(logger: Any, error: BaseException, *, operation: str) -> None:
    mapped = friendly_error(error)
    logger.error("operation_failed operation=%s category=%s exception=%s", operation, mapped.code, error.__class__.__name__)
