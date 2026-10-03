from __future__ import annotations

import logging
import os
import random
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Callable

from .. import __version__
from .binaries import require_ffmpeg
from .errors import (
    AppError,
    CancelledError,
    DownloadFailure,
    ExtractionError,
    StorageError,
    UnsupportedContentError,
    friendly_error,
    safe_log_error,
)
from .logging_setup import SafeYtDlpLogger, get_logger
from .models import (
    MAX_SUBTITLE_LANGUAGES,
    CancelCheck,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    ProgressCallback,
    ProgressEvent,
    PlaylistDownloadRequest,
    PlaylistDownloadResult,
    PlaylistInfo,
    PlaylistMedia,
    PlaylistQuality,
    QueueDownloadRequest,
    QueueDownloadResult,
    QueueItemResult,
    StreamPreference,
    SubtitleSource,
    SubtitleTrackInfo,
    VideoCodecFamily,
    VideoInfo,
    VideoQuality,
    codec_family_of,
    estimate_audio_size_bytes,
)
from .urls import UrlValidationError, normalize_youtube_playlist_url, normalize_youtube_url

SUPPORTED_PLAYLIST_AUDIO_BITRATES = (128, 192, 256, 320)

# A queue entry is a single link, so these are the per-link media modes it can
# run.  Playlist and queue are jobs in their own right and never nest.
_QUEUE_SUPPORTED_MODES = (
    DownloadMode.VIDEO,
    DownloadMode.AUDIO,
    DownloadMode.THUMBNAIL,
    DownloadMode.SUBTITLES,
)

# Pause between caption fetches when a run covers several languages.  YouTube's
# timedtext endpoint begins answering HTTP 429 partway through an unpaced run.
CAPTION_REQUEST_INTERVAL = 0.6

# Share of one item's slice of the progress bar spent reading its details.
# Reading takes seconds and saving takes minutes, so the bar has to be told
# them apart: without the split, a probe that reports itself finished would
# consume the whole slice and the bar would then sit frozen for the download.
_ITEM_PROBE_SHARE = 0.15

# A clean filename is the default: "Alan Walker - Faded.mp3", not
# "Alan Walker - Faded [60ItHLz5WEA].mp3".  The id-bearing form is kept only
# for the one case that needs it -- a title already present in the folder.
# Dropping the id unconditionally would give two different videos both called
# "Official Video" the same name, and with `overwrites: False` the second would
# report itself already downloaded rather than saving a file.  Both forms live
# here because they used to be written out twice and could drift apart.
CLEAN_OUTTMPL = "%(title).180B.%(ext)s"
ID_OUTTMPL = "%(title).180B [%(id)s].%(ext)s"


def item_slice_progress(
    item_index: int,
    total: int,
    item_percent: float,
    *,
    probing: bool,
) -> float:
    """Where one item's work sits on a whole-job 0-100% progress bar.

    ``item_index`` is zero-based.  ``item_percent`` is 0-100 for the step in
    progress, either reading details or saving media.
    """

    offset = 0.0 if probing else _ITEM_PROBE_SHARE
    span = _ITEM_PROBE_SHARE if probing else 1.0 - _ITEM_PROBE_SHARE
    return (item_index + offset + span * item_percent / 100.0) / total * 100.0


def _retry_backoff(attempt: int) -> float:
    """Seconds to wait before retry number ``attempt`` (zero-based).

    Jitter is applied so several retries of the same file do not line up and
    arrive together, which is what makes a client look like an attack.
    """

    return min(2.0**attempt, 30.0) + random.uniform(0.0, 0.75)


def quality_matches_target(quality: VideoQuality, target: PlaylistQuality) -> bool:
    """Return whether a concrete video quality matches a shared choice."""

    if quality.height != target.height:
        return False
    return target.fps is None or round(quality.fps or 0.0, 3) == round(target.fps, 3)


def select_playlist_video_quality(
    video: VideoInfo,
    target: PlaylistQuality,
) -> VideoQuality | None:
    """Pick the best quality for one video under a playlist-wide choice.

    The exact choice wins.  When it is missing, the nearest lower quality is
    used so a whole playlist can still be saved with one setting.
    """

    qualities = video.qualities
    if not qualities:
        return None
    exact = [quality for quality in qualities if quality_matches_target(quality, target)]
    if exact:
        return max(exact, key=lambda item: item.sort_key)

    same_height = [quality for quality in qualities if quality.height == target.height]
    if target.fps is not None:
        lower_fps = [
            quality
            for quality in same_height
            if quality.fps is None or quality.fps <= target.fps + 0.05
        ]
        if lower_fps:
            return max(lower_fps, key=lambda item: item.sort_key)
    elif same_height:
        return max(same_height, key=lambda item: item.sort_key)

    lower_height = [quality for quality in qualities if quality.height < target.height]
    if lower_height:
        return max(lower_height, key=lambda item: item.sort_key)
    # If every available stream is above the requested height, use the
    # smallest available stream rather than silently choosing the largest.
    return min(qualities, key=lambda item: item.sort_key)


def playlist_video_size_bytes(
    video: VideoInfo,
    quality: PlaylistQuality,
    preference: StreamPreference = StreamPreference.QUALITY,
) -> int | None:
    """Return the expected file size for one video under a shared choice.

    ``preference`` picks which of the two streams the row is costed against, so
    the per-video column shows the number the job will actually produce.
    """

    if not video.audio_available:
        return None
    selected = select_playlist_video_quality(video, quality)
    if selected is None:
        return None
    size = selected.size_for_preference(preference)
    return int(size) if size and size > 0 else None


def playlist_audio_size_bytes(video: VideoInfo, bitrate: int | None) -> int | None:
    """Return the expected MP3 size for one video at a shared bitrate."""

    if not video.audio_available:
        return None
    return estimate_audio_size_bytes(video.duration, bitrate)


def select_playlist_videos(
    videos: tuple[VideoInfo, ...] | list[VideoInfo],
    selected_video_ids: tuple[str, ...] | list[str] | None,
) -> list[VideoInfo]:
    """Apply a per-video selection, preserving playlist order.

    ``None`` means every video is selected.  Duplicate titles are irrelevant
    because selection is keyed by the stable YouTube video id.
    """

    if selected_video_ids is None:
        return list(videos)
    wanted = {str(value) for value in selected_video_ids}
    return [video for video in videos if video.video_id in wanted]

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP
    from yt_dlp.utils import PostProcessingError
except ImportError:  # pragma: no cover - exercised only in an incomplete installation
    YoutubeDL = None  # type: ignore[assignment,misc]
    EmbedThumbnailPP = None  # type: ignore[assignment,misc]
    PostProcessingError = None  # type: ignore[assignment,misc]


if EmbedThumbnailPP is not None:

    class _TolerantEmbedThumbnail(EmbedThumbnailPP):  # type: ignore[misc,valid-type]
        """Attach the cover art, but never fail a finished download over it.

        yt-dlp re-raises a postprocessor error and fails the whole download,
        which is the wrong trade here: by the time this runs the MP3 exists, is
        complete, and plays.  Losing the artwork is worth a warning; losing the
        file the user asked for is not.

        The temporary file is removed on the way out, because the base class
        only renames it into place on success and would otherwise leave a
        partial ``.temp.mp3`` beside the real one.
        """

        def run(self, info: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
            try:
                return super().run(info)
            except PostProcessingError as error:  # type: ignore[misc]
                temporary = Path(str(info["filepath"])).with_suffix(".temp.mp3")
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                self.report_warning(f"the cover art could not be embedded: {error}")
                return [], info

else:  # pragma: no cover - only in an installation without yt-dlp
    _TolerantEmbedThumbnail = None  # type: ignore[assignment,misc]


class _ProgressRelay:
    def __init__(self, callback: ProgressCallback | None, cancel_check: CancelCheck | None) -> None:
        self._callback = callback
        self._cancel_check = cancel_check
        self._last_emit: float | None = None
        self._last_signature: tuple[str, float | None, str, str | None, str | None] | None = None

    def check_cancelled(self) -> None:
        if self._cancel_check and self._cancel_check():
            raise CancelledError()

    def is_cancelled(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

    def emit(
        self,
        phase: str,
        percent: float | None,
        message: str,
        *,
        speed: str | None = None,
        eta: str | None = None,
        force: bool = False,
    ) -> None:
        self.check_cancelled()
        if not self._callback:
            return
        now = time.monotonic()
        normalized_percent = None if percent is None else max(0.0, min(100.0, percent))
        signature = (phase, normalized_percent, message, speed, eta)
        # yt-dlp can call progress hooks for every fragment/chunk.  A strict
        # time-based limit prevents a flood of queued Qt signals, especially
        # when a fragment has no total byte count and percent is None.
        if not force:
            if self._last_emit is not None and now - self._last_emit < 0.10:
                return
            if signature == self._last_signature:
                return
        self._last_emit = now
        self._last_signature = signature
        try:
            self._callback(ProgressEvent(phase, normalized_percent, message, speed, eta))
        except Exception:
            # A presentation callback must never interrupt a download.
            pass


class Engine:
    """The single yt-dlp integration point for probing and downloading."""

    def __init__(
        self,
        *,
        ffmpeg_path: Path | None = None,
        app_root: Path | None = None,
        logger: logging.Logger | None = None,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._app_root = app_root
        self._logger = logger or get_logger()
        self._ydl_factory = ydl_factory

    def _new_ydl(self, options: dict[str, Any]) -> Any:
        if self._ydl_factory is not None:
            return self._ydl_factory(options)
        if YoutubeDL is None:
            from .errors import DependencyError

            raise DependencyError("yt-dlp is not installed. Install the pinned dependencies and try again.")
        return YoutubeDL(options)

    def _base_options(
        self,
        output_dir: Path,
        progress: _ProgressRelay,
        *,
        ffmpeg: Path | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "logger": SafeYtDlpLogger(self._logger),
            "socket_timeout": 20,
            "retries": 5,
            "fragment_retries": 8,
            "file_access_retries": 3,
            # Without a delay, yt-dlp spends its whole retry budget in a few
            # milliseconds, which cannot outlast a rate limit.  Backing off
            # turns "too many requests" into a slow success.
            "retry_sleep_functions": {
                "http": _retry_backoff,
                "fragment": _retry_backoff,
                "file_access": lambda attempt: 0.5,
                "extractor": lambda attempt: min(2.0**attempt, 30.0),
            },
            "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "trim_file_name": 180,
            "overwrites": False,
            "cachedir": False,
            "paths": {"home": str(output_dir), "temp": str(output_dir / ".parts")},
            "outtmpl": {"default": str(output_dir / CLEAN_OUTTMPL)},
            "progress_hooks": [self._yt_dlp_progress_hook(progress)],
            "postprocessor_hooks": [self._postprocessor_hook(progress)],
        }
        if ffmpeg is not None:
            options["ffmpeg_location"] = str(ffmpeg)
        return options

    @staticmethod
    def _format_speed(speed: Any) -> str | None:
        try:
            value = float(speed)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
        index = 0
        while value >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        return f"{value:.1f} {units[index]}"

    @staticmethod
    def _format_eta(eta: Any) -> str | None:
        try:
            seconds = int(eta)
        except (TypeError, ValueError):
            return None
        if seconds < 0:
            return None
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d} remaining"
        return f"{minutes:d}:{seconds:02d} remaining"

    def _yt_dlp_progress_hook(self, progress: _ProgressRelay) -> Callable[[dict[str, Any]], None]:
        def hook(data: dict[str, Any]) -> None:
            progress.check_cancelled()
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes") or 0
                percent = None
                try:
                    if total:
                        percent = float(downloaded) / float(total) * 100
                except (TypeError, ValueError, ZeroDivisionError):
                    percent = None
                progress.emit(
                    "downloading",
                    percent,
                    "Downloading media",
                    speed=self._format_speed(data.get("speed")),
                    eta=self._format_eta(data.get("eta")),
                )
            elif status == "finished":
                progress.emit("processing", None, "Processing media", force=True)
            elif status == "error":
                progress.emit("processing", None, "Finishing media", force=True)

        return hook

    def _postprocessor_hook(self, progress: _ProgressRelay) -> Callable[[dict[str, Any]], None]:
        def hook(data: dict[str, Any]) -> None:
            progress.check_cancelled()
            status = data.get("status")
            if status == "started":
                progress.emit("processing", None, "Processing media", force=True)
            elif status == "processing":
                progress.emit("processing", None, "Processing media")
            elif status == "finished":
                progress.emit("finalizing", 100.0, "Finalizing file", force=True)

        return hook

    def probe(
        self,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> VideoInfo:
        relay = _ProgressRelay(progress, cancel_check)
        relay.emit("probing", 0.0, "Fetching video details", force=True)
        try:
            normalized_url = normalize_youtube_url(raw_url)
            options = self._base_options(Path.cwd(), relay)
            options.update({"skip_download": True, "extract_flat": False})
            ydl = self._new_ydl(options)
            with ydl:
                raw_info = ydl.extract_info(normalized_url, download=False)
            relay.check_cancelled()
            info = self._build_video_info(raw_info, normalized_url)
            relay.emit("probing", 100.0, "Video details ready", force=True)
            self._logger.info("probe_completed qualities=%d thumbnail=%s", len(info.qualities), bool(info.thumbnail_url))
            return info
        except UrlValidationError as error:
            raise ExtractionError(str(error), code="invalid_url") from error
        except AppError:
            raise
        except Exception as error:
            safe_log_error(self._logger, error, operation="probe")
            raise friendly_error(error) from error

    def probe_playlist(
        self,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> PlaylistInfo:
        """Probe an explicitly selected playlist for sequential MP4 downloads."""

        relay = _ProgressRelay(progress, cancel_check)
        relay.emit("probing", 0.0, "Fetching playlist details", force=True)
        try:
            normalized_url = normalize_youtube_playlist_url(raw_url)
            options = self._base_options(Path.cwd(), relay)
            options.update({"skip_download": True, "extract_flat": False, "noplaylist": False})
            ydl = self._new_ydl(options)
            with ydl:
                raw_info = ydl.extract_info(normalized_url, download=False)
                relay.check_cancelled()
                info = self._build_playlist_info(raw_info, normalized_url, ydl, relay)
            relay.emit("probing", 100.0, "Playlist details ready", force=True)
            self._logger.info(
                "playlist_probe_completed videos=%d skipped=%d qualities=%d",
                info.video_count,
                info.skipped_count,
                len(info.qualities),
            )
            return info
        except UrlValidationError as error:
            raise ExtractionError(str(error), code="invalid_url") from error
        except AppError:
            raise
        except Exception as error:
            safe_log_error(self._logger, error, operation="playlist_probe")
            raise friendly_error(error) from error

    def _build_video_info(self, raw_info: Any, normalized_url: str) -> VideoInfo:
        if not isinstance(raw_info, dict):
            raise ExtractionError("YouTube returned no video information.", code="empty_metadata")
        if raw_info.get("_type") in {"playlist", "multi_video"}:
            raise UnsupportedContentError("Playlist and channel links are not supported in this version.")
        if raw_info.get("entries") is not None and not raw_info.get("formats"):
            raise UnsupportedContentError("Playlist and channel links are not supported in this version.")

        video_id = str(raw_info.get("id") or "").strip()
        if not video_id:
            raise ExtractionError("YouTube returned a video without a usable identifier.", code="missing_video_id")
        title = " ".join(str(raw_info.get("title") or "YouTube video").split())
        duration = self._as_float(raw_info.get("duration"))
        qualities = self._build_qualities(raw_info.get("formats") or [], duration=duration)
        audio_available = any(
            isinstance(fmt, dict) and str(fmt.get("acodec") or "none").lower() != "none"
            for fmt in raw_info.get("formats") or []
        )
        if not audio_available and str(raw_info.get("acodec") or "none").lower() != "none":
            audio_available = True
        thumbnail_url = self._largest_thumbnail(raw_info.get("thumbnails"))
        if not thumbnail_url and raw_info.get("thumbnail"):
            thumbnail_url = str(raw_info["thumbnail"])
        subtitle_tracks = self._build_subtitle_tracks(raw_info)
        return VideoInfo(
            video_id=video_id,
            title=title,
            duration=duration,
            thumbnail_url=thumbnail_url,
            qualities=tuple(qualities),
            audio_available=audio_available,
            is_live=bool(raw_info.get("is_live")),
            normalized_url=normalized_url,
            subtitles_available=bool(subtitle_tracks),
            subtitle_tracks=subtitle_tracks,
        )

    @staticmethod
    def _build_subtitle_tracks(raw_info: dict[str, Any]) -> tuple[SubtitleTrackInfo, ...]:
        """Collect the caption tracks yt-dlp advertised for this video.

        Auto-generated tracks are only usable when the owner allows them, so
        they are reported separately and the UI can explain the difference.
        """

        tracks: list[SubtitleTrackInfo] = []
        seen: set[tuple[str, bool]] = set()
        for field, automatic in (("subtitles", False), ("automatic_captions", True)):
            container = raw_info.get(field)
            if not isinstance(container, dict):
                continue
            for language_code, entries in container.items():
                code = str(language_code or "").strip()
                if not code or not isinstance(entries, list) or not entries:
                    continue
                if (code, automatic) in seen:
                    continue
                seen.add((code, automatic))
                name = code
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name"):
                        name = str(entry["name"])
                        break
                tracks.append(SubtitleTrackInfo(language_code=code, language_name=name, automatic=automatic))
        return tuple(tracks)

    @staticmethod
    def _subtitle_language_options(request: DownloadRequest, info: VideoInfo) -> list[str]:
        """Decide which language codes yt-dlp should be asked to write.

        ``SubtitleSource.AUTOMATIC`` targets the auto-generated tracks only,
        ``PREFERRED`` targets author-written tracks and falls back to automatic
        ones, and ``ALL`` writes every advertised track.

        Every list derived from the advertised tracks is capped.  YouTube lists
        well over a hundred of them, and every one past the video's own language
        is a server-side machine translation, which the caption endpoint serves
        far more reluctantly than the original: an uncapped run is refused part
        way through.  A language the person picked by hand is never capped.
        """

        if request.subtitle_language:
            return [request.subtitle_language]
        automatic = sorted({track.language_code for track in info.automatic_subtitle_tracks})
        if request.subtitle_source is SubtitleSource.AUTOMATIC:
            return automatic[:MAX_SUBTITLE_LANGUAGES]
        if request.subtitle_source is SubtitleSource.ALL:
            codes: list[str] = []
            for track in (*info.manual_subtitle_tracks, *info.automatic_subtitle_tracks):
                if track.language_code not in codes:
                    codes.append(track.language_code)
            return sorted(codes)[:MAX_SUBTITLE_LANGUAGES]
        preferred = sorted({track.language_code for track in info.manual_subtitle_tracks})
        return (preferred or automatic)[:MAX_SUBTITLE_LANGUAGES]

    @staticmethod
    def _subtitle_write_targets(
        request: DownloadRequest, languages: list[str]
    ) -> tuple[bool, bool]:
        """Whether to write author-written captions, auto-generated ones, or both.

        yt-dlp prefers an author-written track whenever ``writesubtitles`` is on,
        so asking for the ASR track alone has to turn that off - otherwise
        "Auto-generated" quietly hands back the human track for every language
        that has one.

        The pair otherwise follows ``subtitle_source``, but "Author-written
        first" is a statement of preference in terms of track *quality*, not a
        filter over track kind, and its ``help_text`` promises to "fall back to
        auto-generated ones when a language has no human track".  So for that
        source the pair is resolved from what the video actually advertises for
        the languages being asked for.

        Without that resolution the promise is not kept in either direction, and
        both failures are silent because nothing is written and the run ends
        with "The requested SRT file was not produced" - a complaint that reads
        like a network problem and is not one:

          * a language YouTube advertises only as a machine translation, under
            the default source, asked yt-dlp for author-written tracks alone;
          * a video with no author-written captions at all, where the fallback
            in _subtitle_language_options picked the automatic codes while the
            flags still forbade reading them.

        Both were reported live on one video advertising 157 tracks.  A
        language present in both sets keeps the human track, which is the whole
        point of the preference, and a deliberate choice of the auto-generated
        or all-tracks source is honoured for a hand-picked language - only the
        *preference* is resolved from what is advertised.
        """

        source = request.subtitle_source
        if source is SubtitleSource.AUTOMATIC:
            return False, True
        if source is SubtitleSource.ALL:
            return True, True
        author = {track.language_code for track in request.info.manual_subtitle_tracks}
        generated = {track.language_code for track in request.info.automatic_subtitle_tracks}
        wanted_author = [language for language in languages if language in author]
        wanted_generated = [language for language in languages if language in generated]
        if wanted_author or not wanted_generated:
            return True, False
        return False, True

    def _folder_snapshot(self, output_dir: Path) -> frozenset[str]:
        """Names present in the output folder, for spotting what a run just wrote."""

        try:
            return frozenset(entry.name for entry in output_dir.iterdir())
        except OSError:
            return frozenset()

    def _caption_files_written_since(
        self,
        request: DownloadRequest,
        extension: str,
        before: frozenset[str],
    ) -> tuple[Path, ...]:
        """Caption files that appeared while this run was in progress.

        Used to rescue a run that YouTube cut short, so a part-finished batch of
        languages is reported instead of thrown away.

        This is a before-and-after diff rather than a search by name.  Matching
        on the filename used to mean repeating the output template's rules here,
        which is how a clean filename silently stopped being found: the marker
        it looked for -- the video id in square brackets -- is only in the
        fallback template.  A diff needs no naming rules at all, so it cannot
        fall behind the template, and it is also stricter than what it replaced:
        a caption file left by an *earlier* run is no longer picked up and
        reported as part of this one.
        """

        output_dir = self._prepare_output_dir(request.output_dir)
        # SubtitleFormat.extension carries the dot and Path.suffix does too, but
        # normalising removes a trap where a mismatch silently reports nothing.
        suffix = f".{extension.lstrip('.').lower()}"
        found: list[Path] = []
        try:
            entries = sorted(output_dir.iterdir())
        except OSError:
            return ()
        for path in entries:
            if path.name in before or path.suffix.lower() != suffix:
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    found.append(path)
            except OSError:
                continue
        return tuple(found)

    def download_subtitles(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> DownloadResult:
        if request.mode is not DownloadMode.SUBTITLES:
            raise DownloadFailure("The selected download mode is not supported.", code="mode_invalid")
        languages = self._subtitle_language_options(request, request.info)
        if not languages:
            raise DownloadFailure(
                "YouTube did not advertise any caption tracks for this video.",
                code="subtitles_missing",
            )
        ffmpeg = self._ffmpeg_path or require_ffmpeg(self._app_root)
        relay = _ProgressRelay(progress, cancel_check)
        relay.emit("starting", 0.0, "Preparing subtitle download", force=True)
        self._logger.info(
            "download_started mode=subtitles languages=%d automatic=%s",
            len(languages),
            request.subtitle_source is not SubtitleSource.PREFERRED,
        )
        options = self._base_options(request.output_dir, relay, ffmpeg=ffmpeg)
        writesubtitles, writeautomaticsub = self._subtitle_write_targets(request, languages)
        options.update(
            {
                # Captions only: the media itself is not saved.
                "skip_download": True,
                "writesubtitles": writesubtitles,
                "writeautomaticsub": writeautomaticsub,
                "subtitlesformat": request.subtitle_format.value,
                "subtitleslangs": languages,
                # YouTube's caption endpoint starts answering HTTP 429 when a
                # long run arrives faster than it expects, so a multi-language
                # request is paced.  A single language needs no delay.
                "sleep_interval_subtitles": CAPTION_REQUEST_INTERVAL if len(languages) > 2 else 0,
                "postprocessors": [],
            }
        )
        output_dir = self._prepare_output_dir(request.output_dir)
        # Taken before the run so a cut-short one can be told exactly which
        # caption files it managed to write.
        already_there = self._folder_snapshot(output_dir)
        try:
            return self._download_with_ydl(request, options, relay, request.subtitle_format.extension)
        except CancelledError:
            raise
        except DownloadFailure as error:
            # A multi-language run can be cut short when YouTube starts refusing
            # caption requests.  The files already written are still what the
            # person asked for, so they are reported with a note about the cap
            # instead of being presented as a total failure.
            written = self._caption_files_written_since(
                request, request.subtitle_format.extension, already_there
            )
            if not written:
                raise
            self._logger.warning(
                "subtitles_partial languages_saved=%d reason=%s",
                len(written),
                error.code or "unknown",
            )
            relay.emit(
                "completed",
                100.0,
                f"Saved {len(written)} caption files before YouTube stopped serving more",
                force=True,
            )
            return DownloadResult(
                path=written[0],
                mode=request.mode,
                subtitle_paths=written,
                warning=(
                    f"YouTube stopped responding partway through, so {len(written)} of "
                    f"{len(languages)} caption files were saved."
                ),
            )

    def _build_playlist_info(
        self,
        raw_info: Any,
        normalized_url: str,
        ydl: Any,
        relay: _ProgressRelay,
    ) -> PlaylistInfo:
        if not isinstance(raw_info, dict) or raw_info.get("_type") not in {"playlist", "multi_video"}:
            raise UnsupportedContentError("Playlist mode requires a YouTube playlist link.")
        raw_entries = raw_info.get("entries")
        if raw_entries is None or isinstance(raw_entries, (str, bytes)):
            raise ExtractionError("YouTube returned no playlist entries.", code="empty_playlist")
        try:
            entries = list(raw_entries)
        except TypeError as error:
            raise ExtractionError("YouTube returned no playlist entries.", code="empty_playlist") from error
        relay.check_cancelled()
        if not entries:
            raise ExtractionError("YouTube returned an empty playlist.", code="empty_playlist")

        videos: list[VideoInfo] = []
        skipped_count = 0
        total_entries = len(entries)
        for index, entry in enumerate(entries):
            relay.check_cancelled()
            if total_entries:
                relay.emit(
                    "probing",
                    index / total_entries * 100.0,
                    f"Reading playlist video {index + 1} of {total_entries}",
                )
            try:
                video = self._resolve_playlist_entry(entry, ydl, relay)
            except CancelledError:
                raise
            except Exception as error:
                skipped_count += 1
                safe_log_error(self._logger, error, operation="playlist_probe_item")
                continue
            if video is None:
                skipped_count += 1
                continue
            videos.append(video)

        if not videos or all(not video.selectable for video in videos):
            raise ExtractionError(
                "No downloadable video metadata was found in this playlist.",
                code="empty_playlist",
            )
        qualities = self._build_playlist_qualities(
            [video for video in videos if video.selectable]
        )
        title = " ".join(str(raw_info.get("title") or "YouTube playlist").split())
        playlist_id = str(raw_info.get("id") or "").strip()
        if not playlist_id:
            playlist_id = parse_qs(urlsplit(normalized_url).query).get("list", ["playlist"])[0].strip() or "playlist"
        return PlaylistInfo(
            playlist_id=playlist_id,
            title=title,
            videos=tuple(videos),
            qualities=tuple(qualities),
            normalized_url=normalized_url,
            skipped_count=skipped_count,
        )

    def _resolve_playlist_entry(
        self,
        entry: Any,
        ydl: Any,
        relay: _ProgressRelay,
    ) -> VideoInfo | None:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or entry.get("_type") in {"playlist", "multi_video"}:
            return None
        entry_url = self._playlist_entry_url(entry)
        if entry_url is None:
            return None

        availability, availability_reason = self._playlist_entry_availability(entry)
        if availability == "unavailable":
            return self._unavailable_playlist_entry(entry, entry_url, availability_reason)
        if availability == "private":
            return self._unavailable_playlist_entry(entry, entry_url, "Private video")

        if entry.get("formats"):
            info = self._build_video_info(entry, entry_url)
        elif ydl is None:
            return None
        else:
            try:
                raw_info = ydl.extract_info(entry_url, download=False)
            except Exception as error:
                return self._unavailable_playlist_entry(
                    entry, entry_url, friendly_error(error).message
                )
            relay.check_cancelled()
            if not isinstance(raw_info, dict) or raw_info.get("_type") in {"playlist", "multi_video"}:
                return None
            try:
                info = self._build_video_info(raw_info, entry_url)
            except AppError as error:
                return self._unavailable_playlist_entry(entry, entry_url, error.message)
        if not info.qualities:
            return self._unavailable_playlist_entry(entry, entry_url, "No downloadable video quality")
        if not info.audio_available:
            return self._unavailable_playlist_entry(
                entry,
                entry_url,
                "No downloadable audio track, so a complete file cannot be created",
            )
        return info

    @staticmethod
    def _playlist_entry_availability(entry: dict[str, Any]) -> tuple[str, str]:
        """Read yt-dlp's flat availability markers without probing the network."""

        availability = str(entry.get("availability") or "").strip().lower()
        if availability in {"private", "premium_only", "subscriber_only", "needs_auth", "unlisted"}:
            return "unavailable", "Private or restricted video"
        if availability in {"unavailable", "deleted", "removed"}:
            return "unavailable", "This video is no longer available"
        if entry.get("is_live") and not entry.get("duration"):
            return "unavailable", "Live streams cannot be downloaded"
        return "available", ""

    @staticmethod
    def _unavailable_playlist_entry(
        entry: dict[str, Any],
        entry_url: str,
        reason: str,
    ) -> VideoInfo | None:
        """Keep an entry visible in the list so the user can see why it is off."""

        video_id = str(entry.get("id") or "").strip()
        if not video_id:
            return None
        title = " ".join(str(entry.get("title") or video_id).split())
        return VideoInfo(
            video_id=video_id,
            title=title,
            duration=Engine._as_float(entry.get("duration")),
            thumbnail_url=None,
            qualities=(),
            audio_available=False,
            is_live=bool(entry.get("is_live")),
            normalized_url=entry_url,
            selectable=False,
            unavailable_reason=reason or "This video cannot be downloaded",
        )

    @staticmethod
    def _strip_playlist_context(url: str) -> str:
        parsed = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"list", "index", "playlist", "start_radio", "si"}
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    @classmethod
    def _playlist_entry_url(cls, entry: dict[str, Any]) -> str | None:
        for key in ("webpage_url", "original_url", "url"):
            value = entry.get(key)
            if not value:
                continue
            try:
                return cls._strip_playlist_context(normalize_youtube_url(str(value)))
            except UrlValidationError:
                continue
        video_id = str(entry.get("id") or "").strip()
        if video_id:
            try:
                return normalize_youtube_url(f"https://www.youtube.com/watch?v={video_id}")
            except UrlValidationError:
                return None
        return None

    @classmethod
    def _build_playlist_qualities(cls, videos: list[VideoInfo]) -> list[PlaylistQuality]:
        by_key: dict[tuple[int, float], list[VideoQuality]] = {}
        for video in videos:
            seen_keys: set[tuple[int, float]] = set()
            for quality in video.qualities:
                key = (quality.height, round(quality.fps or 0.0, 3))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                by_key.setdefault(key, []).append(quality)

        result: list[PlaylistQuality] = []
        total = len(videos)
        for (height, fps_key), matches in by_key.items():
            estimated_size: int | None = None
            if len(matches) == total and all(
                quality.estimated_size_bytes is not None and quality.estimated_size_bytes > 0
                for quality in matches
            ):
                estimated_size = sum(int(quality.estimated_size_bytes or 0) for quality in matches)
            # Only videos that actually offer a smaller stream can contribute to
            # the smaller-file aggregate, so it is reported separately instead of
            # silently mixing best-quality and smaller sizes together.
            smaller_sizes = [
                int(quality.smaller_size_bytes or 0)
                for quality in matches
                if quality.has_smaller_alternative
            ]
            smaller_total = sum(smaller_sizes) if len(smaller_sizes) == total and smaller_sizes else None
            result.append(
                PlaylistQuality(
                    height=height,
                    fps=fps_key or None,
                    estimated_size_bytes=estimated_size,
                    available_video_count=len(matches),
                    total_video_count=total,
                    smaller_size_bytes=smaller_total,
                    smaller_video_count=len(smaller_sizes),
                )
            )
        return sorted(result, key=lambda item: item.sort_key, reverse=True)

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @classmethod
    def _build_qualities(
        cls,
        formats: list[dict[str, Any]],
        duration: float | None = None,
    ) -> list[VideoQuality]:
        best_audio_size = cls._best_audio_size(formats, duration)

        # Candidates are grouped by resolution first because the best-quality
        # stream and the smallest-file stream are two different formats at the
        # same resolution.  Both have to be known before either can be offered.
        grouped: dict[tuple[int, float], list[tuple[int, dict[str, Any]]]] = {}
        for index, fmt in enumerate(formats):
            if not isinstance(fmt, dict):
                continue
            format_id = str(fmt.get("format_id") or "").strip()
            vcodec = str(fmt.get("vcodec") or "none").lower()
            if not format_id or vcodec in {"none", ""}:
                continue
            height = cls._as_float(fmt.get("height"))
            if not height:
                continue
            fps = cls._format_fps(fmt)
            key = (int(round(height)), round(fps or 0.0, 3))
            grouped.setdefault(key, []).append((index, fmt))

        result: list[VideoQuality] = []
        for key, candidates in grouped.items():
            best = cls._best_quality_candidate(
                candidates,
                duration=duration,
                best_audio_size=best_audio_size,
            )
            if best is None:
                continue
            quality = best
            smaller = cls._smallest_mp4_candidate(
                candidates,
                duration=duration,
                best_audio_size=best_audio_size,
                exclude_format_id=quality.format_id,
                reference_size=quality.estimated_size_bytes,
            )
            if smaller is not None:
                smaller_quality, smaller_family = smaller
                quality = replace(
                    quality,
                    smaller_format_id=smaller_quality.format_id,
                    smaller_size_bytes=smaller_quality.estimated_size_bytes,
                    smaller_codec=smaller_family.value,
                )
            result.append(quality)
        return sorted(result, key=lambda item: item.sort_key, reverse=True)

    @classmethod
    def _best_quality_candidate(
        cls,
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        duration: float | None,
        best_audio_size: int | None,
    ) -> VideoQuality | None:
        best: tuple[tuple[float, float, int, int, int], VideoQuality] | None = None
        for index, fmt in candidates:
            quality = cls._quality_from_format(fmt, index, duration, best_audio_size)
            if quality is None:
                continue
            quality_score = cls._as_float(fmt.get("quality")) or -1.0
            bitrate_score = cls._as_float(fmt.get("tbr") or fmt.get("vbr")) or 0.0
            size_score = int(cls._as_float(fmt.get("filesize") or fmt.get("filesize_approx")) or 0)
            has_audio = 1 if quality.has_audio else 0
            score = (quality_score, bitrate_score, size_score, has_audio, -index)
            if best is None or score > best[0]:
                best = (score, quality)
        return best[1] if best else None

    @classmethod
    def _smallest_mp4_candidate(
        cls,
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        duration: float | None,
        best_audio_size: int | None,
        exclude_format_id: str,
        reference_size: int | None,
    ) -> tuple[VideoQuality, VideoCodecFamily] | None:
        """Smallest stream at this resolution that needs no video re-encode.

        The app always writes MP4, so only an MP4 stream that is already H.264
        can be placed in the output untouched.  A WebM/VP9 or AV1 stream is
        smaller but would have to be transcoded, which costs time and a
        generation of quality, so it is not offered as a smaller file.
        """

        if not reference_size or reference_size <= 0:
            return None
        best: tuple[VideoQuality, VideoCodecFamily] | None = None
        for index, fmt in candidates:
            if str(fmt.get("format_id") or "").strip() == exclude_format_id:
                continue
            if not cls._is_mp4_h264(fmt):
                continue
            quality = cls._quality_from_format(fmt, index, duration, best_audio_size)
            if quality is None:
                continue
            size = quality.estimated_size_bytes
            if not size or size <= 0 or size >= reference_size:
                continue
            if best is None or size < (best[0].estimated_size_bytes or 0):
                best = (quality, codec_family_of(str(fmt.get("vcodec") or "")))
        return best

    @staticmethod
    def _is_mp4_h264(fmt: dict[str, Any]) -> bool:
        extension = str(fmt.get("ext") or "").strip().lower()
        if extension not in {"mp4", "m4v"}:
            return False
        return codec_family_of(str(fmt.get("vcodec") or "")) is VideoCodecFamily.H264

    @classmethod
    def _quality_from_format(
        cls,
        fmt: dict[str, Any],
        index: int,
        duration: float | None,
        best_audio_size: int | None,
    ) -> VideoQuality | None:
        format_id = str(fmt.get("format_id") or "").strip()
        vcodec = str(fmt.get("vcodec") or "none").lower()
        if not format_id or vcodec in {"none", ""}:
            return None
        height = cls._as_float(fmt.get("height"))
        if not height:
            return None
        fps = cls._format_fps(fmt)
        has_audio = str(fmt.get("acodec") or "none").lower() != "none"
        video_size = cls._estimate_format_size(fmt, duration)
        estimated_size = video_size
        if not has_audio:
            estimated_size = (
                video_size + best_audio_size
                if video_size is not None and best_audio_size is not None
                else None
            )
        return VideoQuality(
            format_id=format_id,
            height=int(round(height)),
            fps=fps,
            has_audio=has_audio,
            width=int(cls._as_float(fmt.get("width")) or 0) or None,
            extension=str(fmt.get("ext") or "") or None,
            estimated_size_bytes=estimated_size,
        )

    @classmethod
    def _estimate_format_size(cls, fmt: dict[str, Any], duration: float | None) -> int | None:
        for key in ("filesize", "filesize_approx"):
            size = cls._as_float(fmt.get(key))
            if size is not None and size > 0:
                return int(size)
        bitrate = cls._as_float(fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr"))
        if bitrate and bitrate > 0 and duration and duration > 0:
            # yt-dlp reports bitrate in kbit/s; this is only a fallback when
            # Content-Length/filesize metadata is unavailable.
            return int(duration * bitrate * 1000 / 8)
        return None

    @classmethod
    def _best_audio_size(cls, formats: list[dict[str, Any]], duration: float | None) -> int | None:
        candidates: list[tuple[tuple[int, float, float, int, int], int]] = []
        for index, fmt in enumerate(formats):
            if not isinstance(fmt, dict):
                continue
            vcodec = str(fmt.get("vcodec") or "none").lower()
            acodec = str(fmt.get("acodec") or "none").lower()
            if vcodec not in {"none", ""} or acodec in {"none", ""}:
                continue
            size = cls._estimate_format_size(fmt, duration)
            if size is None:
                continue
            extension = str(fmt.get("ext") or "").lower()
            m4a_preference = 1 if extension == "m4a" else 0
            quality_score = cls._as_float(fmt.get("quality")) or 0.0
            bitrate_score = cls._as_float(fmt.get("abr") or fmt.get("tbr")) or 0.0
            score = (m4a_preference, quality_score, bitrate_score, size, -index)
            candidates.append((score, size))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _format_fps(fmt: dict[str, Any]) -> float | None:
        fps = Engine._as_float(fmt.get("fps"))
        if fps and fps > 0:
            return fps
        note = str(fmt.get("format_note") or "")
        match = re.search(r"(\d{2,3}(?:\.\d+)?)\s*fps", note, re.IGNORECASE)
        if match:
            return Engine._as_float(match.group(1))
        return None

    @staticmethod
    def _largest_thumbnail(thumbnails: Any) -> str | None:
        if not isinstance(thumbnails, list):
            return None
        candidates: list[tuple[int, int, int, str]] = []
        for index, thumbnail in enumerate(thumbnails):
            if not isinstance(thumbnail, dict) or not thumbnail.get("url"):
                continue
            width = Engine._as_float(thumbnail.get("width")) or 0
            height = Engine._as_float(thumbnail.get("height")) or 0
            preference = Engine._as_float(thumbnail.get("preference")) or 0
            candidates.append((int(width * height), int(preference), -index, str(thumbnail["url"])))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return candidates[0][3]

    @staticmethod
    def _safe_stem(title: str, video_id: str) -> str:
        safe_title = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", title).strip(" .")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_id)).strip(" .")
        stem = safe_title or f"youtube-video-{safe_id or 'unknown'}"
        stem = stem[:150].rstrip(" .")
        reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
        reserved.update(f"COM{index}" for index in range(1, 10))
        reserved.update(f"LPT{index}" for index in range(1, 10))
        if stem.split(".", 1)[0].upper() in reserved:
            stem = f"_{stem}"
        return stem

    def _prepare_output_dir(self, output_dir: Path) -> Path:
        try:
            output_dir = output_dir.expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError("The destination folder could not be created or accessed.") from error
        if not output_dir.is_dir():
            raise StorageError("The selected destination is not a folder.")
        return output_dir

    def _video_format_selector(self, quality: VideoQuality, preference: StreamPreference) -> str:
        format_id = quality.for_preference(preference)
        if quality.has_audio:
            return format_id
        return f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio"

    def _output_paths_from_result(self, result: dict[str, Any], extension: str) -> list[Path]:
        """Every produced file with the requested extension, in a stable order.

        A subtitle job can write one file per language, so the result may hold
        several paths rather than the single file the media modes produce.  The
        three places a path can hide are all checked because yt-dlp populates
        only the ones that apply to the mode: ``requested_downloads`` for merged
        media, ``filepath`` for a plain download, and ``requested_subtitles`` for
        captions, which are written outside the normal download bookkeeping.
        """

        if not isinstance(result, dict):
            return []
        candidates: list[str] = []

        def _add(value: Any) -> None:
            if value:
                candidates.append(str(value))

        requested = result.get("requested_downloads")
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, dict):
                    _add(item.get("filepath") or item.get("_filename"))
        # A caption run writes no media, so this is where its files are named.
        subtitles = result.get("requested_subtitles")
        if isinstance(subtitles, dict):
            for entry in subtitles.values():
                if isinstance(entry, dict):
                    _add(entry.get("filepath"))
        _add(result.get("filepath") or result.get("_filename"))

        found: list[Path] = []
        seen: set[Path] = set()
        for value in candidates:
            path = Path(value)
            if path in seen:
                continue
            try:
                if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() == extension.lower():
                    seen.add(path)
                    found.append(path)
            except OSError:
                continue
        return found

    def _output_path_from_result(self, result: dict[str, Any], extension: str) -> Path:
        paths = self._output_paths_from_result(result, extension)
        if paths:
            return paths[0]
        raise DownloadFailure(f"The requested {extension.upper()[1:]} file was not produced.", code="missing_output")

    def _clean_name_is_taken(
        self,
        ydl: Any,
        request: DownloadRequest,
        expected_extension: str,
    ) -> bool:
        """Whether the clean filename for this item already exists on disk.

        The name is *asked of yt-dlp* rather than predicted here.  It owns the
        Windows character sanitisation, the 180-character trim, the reserved
        device names, and the `%(title).180B` length-limited form, and a
        hand-rolled copy of those rules would disagree with it somewhere obscure
        -- and the disagreement would look exactly like "no collision".

        A false positive only costs a tidier filename; a false negative costs a
        silently unsaved video, so the check errs towards appending the id.
        """
        info = request.info
        if not info.title:
            return False
        probe = {
            "id": info.video_id,
            "title": info.title,
            "ext": expected_extension.lstrip("."),
        }
        try:
            candidate = ydl.prepare_filename(probe)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            return False
        if not candidate or candidate == "-":
            return False
        path = Path(candidate)
        if path.exists():
            return True
        if request.mode is DownloadMode.SUBTITLES:
            # A caption file carries a language code the media name does not, and
            # it goes *before* the extension -- "Title.en.srt", not "Title.srt".
            # So the exact name above is only one candidate; match on the stem.
            # Two different videos sharing a title and a language really would
            # overwrite each other here.
            return any(path.parent.glob(f"{path.stem}.*"))
        return False

    def _download_with_ydl(
        self,
        request: DownloadRequest,
        options: dict[str, Any],
        relay: _ProgressRelay,
        expected_extension: str,
        *,
        extra_postprocessors: tuple[Any, ...] = (),
    ) -> DownloadResult:
        try:
            download_url = normalize_youtube_url(request.url)
        except UrlValidationError as error:
            raise ExtractionError(str(error), code="invalid_url") from error
        output_dir = self._prepare_output_dir(request.output_dir)
        options["outtmpl"] = {"default": str(output_dir / CLEAN_OUTTMPL)}
        options["paths"] = {"home": str(output_dir), "temp": str(output_dir / ".parts")}
        ydl = self._new_ydl(options)
        if self._clean_name_is_taken(ydl, request, expected_extension):
            # yt-dlp reads the template out of params each time it names a file,
            # so switching it here is enough and saves building a second YoutubeDL.
            ydl.params.setdefault("outtmpl", {})["default"] = str(output_dir / ID_OUTTMPL)
            self._logger.info(
                "filename_disambiguated video_id=%s because_the_title_is_already_taken",
                request.info.video_id,
            )
        # Added as instances rather than named in ``options`` because a caller
        # needs a postprocessor that yt-dlp does not register, and because the
        # order is then explicit: these run after everything the options list
        # declares, which is what "after the conversion" has to mean.
        for factory in extra_postprocessors:
            ydl.add_post_processor(factory(ydl))
        try:
            with ydl:
                result = ydl.extract_info(download_url, download=True)
            relay.check_cancelled()
            if not isinstance(result, dict) or result.get("_type") in {"playlist", "multi_video"}:
                raise UnsupportedContentError("The link resolved to a playlist or channel instead of one video.")
            path = self._output_path_from_result(result, expected_extension)
            relay.emit("completed", 100.0, "Download complete", force=True)
            written: tuple[Path, ...] = ()
            if request.mode is DownloadMode.SUBTITLES:
                # A caption run reports one file per language.  ``subtitle_paths``
                # is the complete set with ``path`` as its first entry, so a
                # caller never has to merge the two by hand.
                written = tuple(self._output_paths_from_result(result, expected_extension))
                if not written:
                    written = (path,)
                if len(written) > 1:
                    relay.emit(
                        "completed",
                        100.0,
                        f"Saved {len(written)} subtitle files",
                        force=True,
                    )
            return DownloadResult(path=path, mode=request.mode, subtitle_paths=written)
        except CancelledError:
            raise
        except Exception as error:
            if relay.is_cancelled():
                raise CancelledError() from error
            safe_log_error(self._logger, error, operation=request.mode.value)
            raise friendly_error(error) from error

    def download_video(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> DownloadResult:
        if request.quality is None:
            raise DownloadFailure("Select a video quality before downloading.", code="quality_missing")
        if not request.info.audio_available:
            raise DownloadFailure("This video has no downloadable audio track, so a complete MP4 cannot be created.", code="audio_missing")
        ffmpeg = self._ffmpeg_path or require_ffmpeg(self._app_root)
        relay = _ProgressRelay(progress, cancel_check)
        relay.emit("starting", 0.0, "Preparing video download", force=True)
        self._logger.info(
            "download_started mode=video quality_height=%d preference=%s",
            request.quality.height,
            request.preference.value,
        )
        options = self._base_options(request.output_dir, relay, ffmpeg=ffmpeg)
        options.update(
            {
                "format": self._video_format_selector(request.quality, request.preference),
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
                "postprocessor_args": {
                    "videoconvertor+ffmpeg": ["-c:v", "libopenh264", "-c:a", "aac"]
                },
            }
        )
        result = self._download_with_ydl(request, options, relay, ".mp4")
        self._logger.info("download_completed mode=video")
        return result

    def download_audio(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> DownloadResult:
        if request.audio_bitrate not in {128, 192, 256, 320}:
            raise DownloadFailure("Choose a supported MP3 bitrate before downloading.", code="bitrate_missing")
        if not request.info.audio_available:
            raise DownloadFailure("This video has no downloadable audio track.", code="audio_missing")
        ffmpeg = self._ffmpeg_path or require_ffmpeg(self._app_root)
        relay = _ProgressRelay(progress, cancel_check)
        relay.emit("starting", 0.0, "Preparing audio download", force=True)
        self._logger.info("download_started mode=audio bitrate=%d embed_cover=%s", request.audio_bitrate, request.embed_cover)
        options = self._base_options(request.output_dir, relay, ffmpeg=ffmpeg)
        if request.embed_cover:
            options.update(
                {
                    "format": "bestaudio[ext=m4a]/bestaudio/best",
                    # The cover art has to be on disk before the postprocessor can
                    # put it in the file, and this is what fetches it.  Only asked
                    # for here: the video and playlist paths have no use for a loose
                    # image, and would leave one beside every download.
                    "writethumbnail": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": str(request.audio_bitrate),
                        },
                    ],
                }
            )
            extra_pps = (
                (_TolerantEmbedThumbnail,) if _TolerantEmbedThumbnail is not None else ()
            )
        else:
            options.update(
                {
                    "format": "bestaudio[ext=m4a]/bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": str(request.audio_bitrate),
                        },
                    ],
                }
            )
            extra_pps = ()
        result = self._download_with_ydl(
            request,
            options,
            relay,
            ".mp3",
            extra_postprocessors=extra_pps,
        )
        self._logger.info("download_completed mode=audio")
        return result

    def download_thumbnail(
        self,
        request: DownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> DownloadResult:
        relay = _ProgressRelay(progress, cancel_check)
        output_dir = self._prepare_output_dir(request.output_dir)
        if not request.info.thumbnail_url:
            raise ExtractionError("YouTube did not provide a thumbnail for this video.", code="thumbnail_missing")
        relay.emit("starting", 0.0, "Preparing thumbnail download", force=True)
        self._logger.info("download_started mode=thumbnail")
        request_data = urllib.request.Request(
            request.info.thumbnail_url,
            headers={"User-Agent": f"ClipDock/{__version__}"},
        )
        temporary_path: Path | None = None
        try:
            with urllib.request.urlopen(request_data, timeout=30) as response:
                total = self._as_float(response.headers.get("Content-Length"))
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                    raise DownloadFailure("The thumbnail response was not an image. Fetch the details again and try again.", code="thumbnail_invalid")
                extension = self._thumbnail_extension(content_type, request.info.thumbnail_url)
                destination = output_dir / f"{self._safe_stem(request.info.title, request.info.video_id)}{extension}"
                destination = self._unique_path(destination)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".thumbnail-",
                    suffix=".part",
                    dir=output_dir,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    downloaded = 0
                    first_chunk = True
                    while True:
                        relay.check_cancelled()
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        if first_chunk:
                            if not self._looks_like_image_bytes(chunk):
                                raise DownloadFailure("The thumbnail response was not a valid image.", code="thumbnail_invalid")
                            first_chunk = False
                        temporary.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > 20 * 1024 * 1024:
                            raise DownloadFailure("The thumbnail response was unexpectedly large.", code="thumbnail_too_large")
                        percent = downloaded / total * 100 if total else None
                        relay.emit("downloading", percent, "Downloading thumbnail")
                    if downloaded == 0:
                        raise DownloadFailure("The thumbnail response was empty.", code="thumbnail_empty")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, destination)
                temporary_path = None
            relay.emit("completed", 100.0, "Download complete", force=True)
            self._logger.info("download_completed mode=thumbnail")
            return DownloadResult(path=destination, mode=DownloadMode.THUMBNAIL)
        except CancelledError:
            raise
        except AppError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            safe_log_error(self._logger, error, operation="thumbnail")
            raise DownloadFailure("The thumbnail could not be downloaded because the network request failed. Check the connection and try again.", code="network_failure") from error
        except OSError as error:
            safe_log_error(self._logger, error, operation="thumbnail")
            raise friendly_error(error) from error
        except Exception as error:
            safe_log_error(self._logger, error, operation="thumbnail")
            raise friendly_error(error) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _looks_like_image_bytes(data: bytes) -> bool:
        return (
            data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )

    @staticmethod
    def _thumbnail_extension(content_type: str, url: str) -> str:
        if "png" in content_type:
            return ".png"
        if "webp" in content_type or ".webp" in url.lower():
            return ".webp"
        return ".jpg"

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        for index in range(1, 10000):
            candidate = path.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists():
                return candidate
        raise StorageError("Could not create a unique output filename.")

    def download_playlist(
        self,
        request: PlaylistDownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> PlaylistDownloadResult:
        """Download the selected playlist videos sequentially.

        One quality (or MP3 bitrate) is shared by the whole job, and a video
        that is missing from ``selected_video_ids`` is not downloaded at all.
        """

        try:
            normalize_youtube_playlist_url(request.url)
        except UrlValidationError as error:
            raise ExtractionError(str(error), code="invalid_url") from error

        media = request.media if isinstance(request.media, PlaylistMedia) else PlaylistMedia.VIDEO
        output_dir = self._prepare_output_dir(request.output_dir)
        videos = [
            video
            for video in select_playlist_videos(request.info.videos, request.selected_video_ids)
            if video.selectable
        ]
        if not videos:
            raise DownloadFailure(
                "Select at least one video in this playlist before downloading.",
                code="no_selection",
            )

        target_quality: PlaylistQuality | None = None
        audio_bitrate: int | None = None
        if media is PlaylistMedia.VIDEO:
            target_quality = request.quality
            if target_quality is None or not isinstance(target_quality, PlaylistQuality):
                raise DownloadFailure("Select a video quality before downloading.", code="quality_missing")
        elif media is PlaylistMedia.AUDIO:
            audio_bitrate = request.audio_bitrate
            if audio_bitrate not in SUPPORTED_PLAYLIST_AUDIO_BITRATES:
                raise DownloadFailure(
                    "Choose a supported MP3 bitrate before downloading.",
                    code="bitrate_missing",
                )

        total = len(videos)
        item_noun = media.item_label
        relay = _ProgressRelay(progress, cancel_check)
        paths: list[Path] = []
        failures: list[tuple[str, str]] = []
        # Kept alongside the failures so the GUI can re-run exactly the videos
        # that did not save, without having to guess an id back out of a title.
        failed_ids: list[str] = []
        fallback_video_count = 0
        relay.emit("starting", 0.0, f"Preparing playlist {item_noun} download", force=True)

        for index, video in enumerate(videos):
            relay.check_cancelled()
            quality: VideoQuality | None = None
            if target_quality is not None:
                quality = select_playlist_video_quality(video, target_quality)
                if quality is None:
                    failures.append((video.title, "No compatible video quality was found."))
                    failed_ids.append(video.video_id)
                    relay.emit(
                        "playlist",
                        (index + 1) / total * 100.0,
                        f"{item_noun.capitalize()} {index + 1} of {total}: no compatible quality",
                    )
                    continue
                if not quality_matches_target(quality, target_quality):
                    fallback_video_count += 1

            if media is PlaylistMedia.SUBTITLES and not video.subtitles_available:
                failures.append(
                    (video.title, "YouTube did not advertise any caption tracks for this video.")
                )
                failed_ids.append(video.video_id)
                relay.emit(
                    "playlist",
                    (index + 1) / total * 100.0,
                    f"{item_noun.capitalize()} {index + 1} of {total}: skipped (no captions)",
                )
                continue

            if media is not PlaylistMedia.SUBTITLES and not video.audio_available:
                message = (
                    "This video has no downloadable audio track."
                    if media is PlaylistMedia.AUDIO
                    else "This video has no downloadable audio track, so a complete MP4 cannot be created."
                )
                failures.append((video.title, message))
                failed_ids.append(video.video_id)
                relay.emit(
                    "playlist",
                    (index + 1) / total * 100.0,
                    f"{item_noun.capitalize()} {index + 1} of {total}: skipped (no audio)",
                )
                continue

            video_url = video.normalized_url.strip()
            if not video_url and video.video_id:
                try:
                    video_url = normalize_youtube_url(f"https://www.youtube.com/watch?v={video.video_id}")
                except UrlValidationError:
                    video_url = ""
            if not video_url:
                failures.append((video.title, "The playlist entry did not contain a valid video URL."))
                failed_ids.append(video.video_id)
                relay.emit(
                    "playlist",
                    (index + 1) / total * 100.0,
                    f"{item_noun.capitalize()} {index + 1} of {total}: skipped (missing video URL)",
                )
                continue

            boundary = index / total * 100.0
            relay.emit(
                "playlist",
                boundary,
                f"{item_noun.capitalize()} {index + 1} of {total}: preparing",
                force=True,
            )

            highest_aggregate = boundary
            probing = True

            def item_progress(event: Any, item_index: int = index) -> None:
                nonlocal highest_aggregate
                raw_percent = getattr(event, "percent", None)
                if raw_percent is None:
                    aggregate = highest_aggregate
                else:
                    try:
                        item_percent = max(0.0, min(100.0, float(raw_percent)))
                    except (TypeError, ValueError):
                        item_percent = 0.0
                    aggregate = item_slice_progress(
                        item_index, total, item_percent, probing=probing
                    )
                    # Emit the clamped value: yt-dlp starts a merged download's
                    # second file at 0%, which would otherwise drag the bar back.
                    highest_aggregate = max(highest_aggregate, aggregate)
                    aggregate = highest_aggregate
                relay.emit(
                    "playlist",
                    aggregate,
                    f"{item_noun.capitalize()} {item_index + 1} of {total}: "
                    f"{getattr(event, 'message', 'Working…')}",
                    speed=getattr(event, "speed", None),
                    eta=getattr(event, "eta", None),
                )

            item_request = DownloadRequest(
                url=video_url,
                output_dir=output_dir,
                mode=DownloadMode.VIDEO
                if media is PlaylistMedia.VIDEO
                else (DownloadMode.AUDIO if media is PlaylistMedia.AUDIO else DownloadMode.SUBTITLES),
                info=video,
                quality=quality,
                audio_bitrate=audio_bitrate,
                preference=request.preference,
                subtitle_format=request.subtitle_format,
                subtitle_source=request.subtitle_source,
                embed_cover=request.embed_cover,
            )
            probing = False
            try:
                if media is PlaylistMedia.AUDIO:
                    result = self.download_audio(
                        item_request,
                        progress=item_progress,
                        cancel_check=cancel_check,
                    )
                elif media is PlaylistMedia.SUBTITLES:
                    result = self.download_subtitles(
                        item_request,
                        progress=item_progress,
                        cancel_check=cancel_check,
                    )
                else:
                    result = self.download_video(
                        item_request,
                        progress=item_progress,
                        cancel_check=cancel_check,
                    )
            except CancelledError:
                raise
            except Exception as error:
                mapped = friendly_error(error)
                if isinstance(mapped, CancelledError):
                    raise mapped
                failures.append((video.title, mapped.message))
                failed_ids.append(video.video_id)
                self._logger.error(
                    "playlist_item_failed index=%d category=%s media=%s",
                    index + 1,
                    mapped.code,
                    media.value,
                )
                relay.emit(
                    "playlist",
                    (index + 1) / total * 100.0,
                    f"{item_noun.capitalize()} {index + 1} of {total}: failed; continuing",
                )
                continue

            paths.append(result.path)
            relay.emit(
                "playlist",
                (index + 1) / total * 100.0,
                f"{item_noun.capitalize()} {index + 1} of {total}: complete",
            )

        if not paths:
            detail = failures[0][1] if failures else "No compatible items were available."
            raise DownloadFailure(
                f"No {item_noun}s could be downloaded from this playlist ({len(failures)} failed). "
                f"First item: {detail}",
                code="playlist_failed",
            )
        relay.emit(
            "completed",
            100.0,
            f"Playlist complete: {len(paths)} of {total} {item_noun}s saved",
            force=True,
        )
        self._logger.info(
            "playlist_download_completed saved=%d failed=%d fallback=%d media=%s",
            len(paths),
            len(failures),
            fallback_video_count,
            media.value,
        )
        return PlaylistDownloadResult(
            paths=tuple(paths),
            total=total,
            failures=tuple(failures),
            failed_video_ids=tuple(failed_ids),
            fallback_video_count=fallback_video_count,
            media=media,
            skipped_count=len(request.info.videos) - total,
        )

    def download_queue(
        self,
        request: QueueDownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> QueueDownloadResult:
        """Download a list of independent links one after another.

        Every link is validated, probed, and downloaded in turn.  A failure on
        one link is recorded and the queue continues, which is the same
        behaviour a playlist already has.  Each link picks its own nearest
        available quality, because links in a queue come from unrelated
        channels and cannot be assumed to share resolutions.
        """

        media = request.media if request.media in _QUEUE_SUPPORTED_MODES else DownloadMode.VIDEO
        output_dir = self._prepare_output_dir(request.output_dir)
        relay = _ProgressRelay(progress, cancel_check)
        total = request.total
        if total == 0:
            raise DownloadFailure("Add at least one link to the queue.", code="queue_empty")

        target_quality = request.quality
        if media is DownloadMode.VIDEO and not isinstance(target_quality, PlaylistQuality):
            raise DownloadFailure("Select a video quality before downloading.", code="quality_missing")
        if media is DownloadMode.AUDIO and request.audio_bitrate not in SUPPORTED_PLAYLIST_AUDIO_BITRATES:
            raise DownloadFailure(
                "Choose a supported MP3 bitrate before downloading.",
                code="bitrate_missing",
            )

        items: list[QueueItemResult] = []
        relay.emit("starting", 0.0, f"Preparing {total} queued link{'s' if total != 1 else ''}", force=True)
        self._logger.info("queue_download_started links=%d media=%s", total, media.value)

        for index, raw_url in enumerate(request.urls):
            relay.check_cancelled()
            position = index + 1
            boundary = index / total * 100.0
            highest_aggregate = boundary
            probing = True

            def item_progress(event: Any, item_index: int = index) -> None:
                nonlocal highest_aggregate
                raw_percent = getattr(event, "percent", None)
                if raw_percent is None:
                    aggregate = highest_aggregate
                else:
                    try:
                        item_percent = max(0.0, min(100.0, float(raw_percent)))
                    except (TypeError, ValueError):
                        item_percent = 0.0
                    aggregate = item_slice_progress(
                        item_index, total, item_percent, probing=probing
                    )
                    # Emit the clamped value: yt-dlp starts a merged download's
                    # second file at 0%, which would otherwise drag the bar back.
                    highest_aggregate = max(highest_aggregate, aggregate)
                    aggregate = highest_aggregate
                relay.emit(
                    "queue",
                    aggregate,
                    f"Link {item_index + 1} of {total}: {getattr(event, 'message', 'Working…')}",
                    speed=getattr(event, "speed", None),
                    eta=getattr(event, "eta", None),
                )

            relay.emit("queue", boundary, f"Link {position} of {total}: reading details", force=True)
            try:
                info = self.probe(raw_url, progress=item_progress, cancel_check=cancel_check)
            except CancelledError:
                raise
            except Exception as error:
                mapped = friendly_error(error)
                if isinstance(mapped, CancelledError):
                    raise mapped
                items.append(
                    QueueItemResult(url=raw_url, index=position, error=mapped.message)
                )
                self._logger.error(
                    "queue_item_probe_failed index=%d category=%s", position, mapped.code
                )
                relay.emit(
                    "queue",
                    position / total * 100.0,
                    f"Link {position} of {total}: failed; continuing",
                )
                continue

            quality: VideoQuality | None = None
            used_fallback = False
            if isinstance(target_quality, PlaylistQuality):
                quality = select_playlist_video_quality(info, target_quality)
                if quality is None:
                    items.append(
                        QueueItemResult(
                            url=raw_url,
                            index=position,
                            title=info.title,
                            error="No compatible video quality was found.",
                        )
                    )
                    relay.emit(
                        "queue",
                        position / total * 100.0,
                        f"Link {position} of {total}: no compatible quality",
                    )
                    continue
                used_fallback = not quality_matches_target(quality, target_quality)

            item_request = DownloadRequest(
                url=info.normalized_url or raw_url,
                output_dir=output_dir,
                mode=media,
                info=info,
                quality=quality,
                audio_bitrate=request.audio_bitrate,
                preference=request.preference,
                subtitle_format=request.subtitle_format,
                subtitle_source=request.subtitle_source,
                embed_cover=request.embed_cover,
            )
            probing = False
            try:
                if media is DownloadMode.AUDIO:
                    result = self.download_audio(
                        item_request, progress=item_progress, cancel_check=cancel_check
                    )
                elif media is DownloadMode.SUBTITLES:
                    result = self.download_subtitles(
                        item_request, progress=item_progress, cancel_check=cancel_check
                    )
                elif media is DownloadMode.THUMBNAIL:
                    result = self.download_thumbnail(
                        item_request, progress=item_progress, cancel_check=cancel_check
                    )
                else:
                    result = self.download_video(
                        item_request, progress=item_progress, cancel_check=cancel_check
                    )
            except CancelledError:
                raise
            except Exception as error:
                mapped = friendly_error(error)
                if isinstance(mapped, CancelledError):
                    raise mapped
                items.append(
                    QueueItemResult(
                        url=raw_url,
                        index=position,
                        title=info.title,
                        error=mapped.message,
                    )
                )
                self._logger.error(
                    "queue_item_failed index=%d category=%s", position, mapped.code
                )
                relay.emit(
                    "queue",
                    position / total * 100.0,
                    f"Link {position} of {total}: failed; continuing",
                )
                continue

            items.append(
                QueueItemResult(
                    url=raw_url,
                    index=position,
                    path=result.path,
                    title=info.title,
                    used_fallback_quality=used_fallback,
                )
            )
            relay.emit(
                "queue",
                position / total * 100.0,
                f"Link {position} of {total}: complete",
            )

        saved = [item for item in items if item.path is not None]
        if not saved:
            detail = items[0].error if items and items[0].error else "No links could be downloaded."
            raise DownloadFailure(
                f"None of the {total} queued links could be downloaded. First item: {detail}",
                code="queue_failed",
            )
        relay.emit(
            "completed",
            100.0,
            f"Queue complete: {len(saved)} of {total} links saved",
            force=True,
        )
        self._logger.info(
            "queue_download_completed saved=%d failed=%d fallback=%d media=%s",
            len(saved),
            len(items) - len(saved),
            sum(1 for item in items if item.used_fallback_quality),
            media.value,
        )
        return QueueDownloadResult(items=tuple(items), media=media)

    def download(
        self,
        request: DownloadRequest | PlaylistDownloadRequest | QueueDownloadRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> DownloadResult | PlaylistDownloadResult | QueueDownloadResult:
        """Dispatch a single-video, playlist, or queued-batch request."""

        if isinstance(request, QueueDownloadRequest):
            return self.download_queue(request, progress=progress, cancel_check=cancel_check)
        if isinstance(request, PlaylistDownloadRequest):
            return self.download_playlist(request, progress=progress, cancel_check=cancel_check)
        if request.mode is DownloadMode.PLAYLIST:
            if isinstance(request.info, PlaylistInfo):
                wants_audio = request.quality is None and request.audio_bitrate is not None
                media = PlaylistMedia.AUDIO if wants_audio else PlaylistMedia.VIDEO
                if media is PlaylistMedia.VIDEO and not isinstance(request.quality, PlaylistQuality):
                    raise DownloadFailure(
                        "Playlist downloads require playlist metadata and a shared quality choice.",
                        code="mode_invalid",
                    )
                playlist_request = PlaylistDownloadRequest(
                    url=request.url,
                    output_dir=request.output_dir,
                    info=request.info,
                    quality=request.quality if isinstance(request.quality, PlaylistQuality) else None,
                    media=media,
                    audio_bitrate=request.audio_bitrate,
                )
                return self.download_playlist(playlist_request, progress=progress, cancel_check=cancel_check)
            raise DownloadFailure(
                "Playlist downloads require playlist metadata.",
                code="mode_invalid",
            )
        try:
            normalize_youtube_url(request.url)
        except UrlValidationError as error:
            raise ExtractionError(str(error), code="invalid_url") from error
        if request.mode is DownloadMode.VIDEO:
            return self.download_video(request, progress=progress, cancel_check=cancel_check)
        if request.mode is DownloadMode.AUDIO:
            return self.download_audio(request, progress=progress, cancel_check=cancel_check)
        if request.mode is DownloadMode.THUMBNAIL:
            return self.download_thumbnail(request, progress=progress, cancel_check=cancel_check)
        if request.mode is DownloadMode.SUBTITLES:
            return self.download_subtitles(request, progress=progress, cancel_check=cancel_check)
        raise DownloadFailure("The selected download mode is not supported.", code="mode_invalid")
