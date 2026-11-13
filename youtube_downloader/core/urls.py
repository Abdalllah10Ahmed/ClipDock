from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class UrlValidationError(ValueError):
    """Raised when a pasted value is not a supported media URL."""


def _normalize_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise UrlValidationError("Paste a media link first.")
    if any(ord(character) < 32 or character.isspace() for character in value):
        raise UrlValidationError("The link contains invalid whitespace or control characters.")

    if "://" not in value:
        value = f"https://{value}"

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as error:
        raise UrlValidationError("The link is not a valid URL.") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UrlValidationError("Use an http:// or https:// link.")
    if parsed.username or parsed.password:
        raise UrlValidationError("The link must not contain embedded credentials.")
    if not host:
        raise UrlValidationError("The link must include a website address.")

    # Drop fragments; keep path/query as provided so site-specific ids survive.
    path = parsed.path or "/"
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))
    return normalized


def normalize_media_url(raw_url: str) -> str:
    """Normalize and validate a media URL for best-effort extraction."""

    return _normalize_url(raw_url)


def normalize_playlist_url(raw_url: str) -> str:
    """Normalize a URL for playlist/collection workflows.

    Playlist support is site- and extractor-dependent; validation stays minimal
    and yt-dlp determines whether the link can be treated as a playlist.
    """

    return _normalize_url(raw_url)


# Compatibility aliases kept while the codebase migrates to generic naming.
def normalize_youtube_url(raw_url: str) -> str:
    """Backward-compatible alias for single-media URL normalization."""

    return normalize_media_url(raw_url)


def normalize_youtube_playlist_url(raw_url: str) -> str:
    """Backward-compatible alias for playlist URL normalization."""

    return normalize_playlist_url(raw_url)

