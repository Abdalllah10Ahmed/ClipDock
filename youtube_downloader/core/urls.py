from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class UrlValidationError(ValueError):
    """Raised when a pasted value is not a supported YouTube URL."""


_ALLOWED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_COLLECTION_PATH_PREFIXES = (
    "/channel/",
    "/c/",
    "/user/",
    "/feed/",
    "/results",
    "/@",
)


def _normalize_youtube_url(raw_url: str, *, allow_playlist: bool) -> str:
    value = (raw_url or "").strip()
    if not value:
        raise UrlValidationError("Paste a YouTube video link first.")
    if any(ord(character) < 32 for character in value):
        raise UrlValidationError("The link contains an invalid control character.")

    if "://" not in value:
        value = f"https://{value}"

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as error:
        raise UrlValidationError("The link is not a valid YouTube URL.") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UrlValidationError("Use an http:// or https:// YouTube link.")
    if parsed.username or parsed.password:
        raise UrlValidationError("The link must not contain embedded credentials.")

    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _ALLOWED_HOSTS):
        raise UrlValidationError("Only YouTube video links are supported.")

    path = (parsed.path or "/").rstrip("/") or "/"
    is_playlist = path == "/playlist" or path.startswith("/playlist/")
    if not is_playlist and any(
        path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _COLLECTION_PATH_PREFIXES
    ):
        raise UrlValidationError("Playlist and channel links are not supported in this version.")
    if is_playlist and not allow_playlist:
        raise UrlValidationError("Playlist and channel links are not supported in this version.")

    segments = [segment for segment in path.split("/") if segment]
    if is_playlist:
        list_ids = parse_qs(parsed.query).get("list", [])
        has_playlist_id = bool(list_ids and list_ids[0].strip())
        if not has_playlist_id:
            raise UrlValidationError("The playlist link does not contain a valid list ID.")
    else:
        is_watch = path == "/watch"
        is_youtu_be = host == "youtu.be" or host.endswith(".youtu.be")
        is_nocookie_embed = (host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")) and segments[:1] == ["embed"]
        if is_watch:
            video_ids = parse_qs(parsed.query).get("v", [])
            has_video_id = bool(video_ids and video_ids[0].strip())
        elif is_youtu_be:
            has_video_id = len(segments) == 1 and bool(segments[0])
        elif is_nocookie_embed:
            has_video_id = len(segments) == 2 and bool(segments[1])
        elif len(segments) == 2 and segments[0] in {"shorts", "embed", "live"}:
            has_video_id = bool(segments[1])
        else:
            has_video_id = False
        if not has_video_id:
            raise UrlValidationError("The link does not identify a single YouTube video.")

    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))
    return normalized


def normalize_youtube_url(raw_url: str) -> str:
    """Normalize and validate a single-video YouTube URL."""

    return _normalize_youtube_url(raw_url, allow_playlist=False)


def normalize_youtube_playlist_url(raw_url: str) -> str:
    """Normalize a playlist URL for the explicit playlist workflow.

    YouTube hands out ``/playlist?list=…`` and ``/watch?v=…&list=…`` for the
    same playlist, and the address bar produces the second form constantly:
    open a playlist, click a video, copy the link.  Any accepted YouTube URL
    that carries a ``list`` parameter therefore resolves to that playlist.
    """

    normalized = _normalize_youtube_url(raw_url, allow_playlist=True)
    parsed = urlsplit(normalized)
    list_ids = parse_qs(parsed.query).get("list", [])
    playlist_id = list_ids[0].strip() if list_ids else ""
    if playlist_id:
        # Rewrite to the canonical playlist form.  The host is normalised to
        # www.youtube.com because a youtu.be or music.youtube.com link does not
        # have a /playlist path on that host.
        return urlunsplit(
            ("https", "www.youtube.com", "/playlist", urlencode({"list": playlist_id}), "")
        )
    if (parsed.path or "").rstrip("/") != "/playlist":
        raise UrlValidationError(
            "Playlist mode requires a YouTube playlist link, or a video link that belongs to one."
        )
    return normalized

