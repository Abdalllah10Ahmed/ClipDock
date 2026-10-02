from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


def format_size(size_bytes: int | float | None) -> str:
    """Return a compact human-readable file size for UI estimates."""
    if size_bytes is None:
        return "size unavailable"
    try:
        value = float(size_bytes)
    except (TypeError, ValueError):
        return "size unavailable"
    if value <= 0:
        return "size unavailable"
    for unit, divisor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if value >= divisor:
            amount = f"{value / divisor:.1f}".rstrip("0").rstrip(".")
            return f"{amount} {unit}"
    return f"{int(value)} B"


class DownloadMode(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    THUMBNAIL = "thumbnail"
    SUBTITLES = "subtitles"
    PLAYLIST = "playlist"
    QUEUE = "queue"


# YouTube advertises a machine-translated caption track for well over a hundred
# languages on most videos.  Fetching all of them is rarely what a person
# means, and the caption endpoint starts answering HTTP 429 partway through,
# which would abandon the run.  The engine therefore asks for at most this many
# languages and says so in the UI rather than failing silently.
MAX_SUBTITLE_LANGUAGES = 25


class SubtitleFormat(str, Enum):
    """Subtitle container written to disk."""

    SRT = "srt"
    VTT = "vtt"

    @property
    def extension(self) -> str:
        return f".{self.value}"

    @property
    def label(self) -> str:
        return "SubRip (SRT)" if self is SubtitleFormat.SRT else "WebVTT (VTT)"


class SubtitleSource(str, Enum):
    """Whether to prefer human-authored captions or include auto-generated ones."""

    PREFERRED = "preferred"
    AUTOMATIC = "automatic"
    ALL = "all"

    @property
    def label(self) -> str:
        return {
            SubtitleSource.PREFERRED: "Author-written first",
            SubtitleSource.AUTOMATIC: "Auto-generated (ASR)",
            SubtitleSource.ALL: "All available tracks",
        }[self]

    @property
    def help_text(self) -> str:
        return {
            SubtitleSource.PREFERRED: (
                "Saves author-written captions and falls back to auto-generated ones when a "
                "language has no human track. Recommended for most videos."
            ),
            SubtitleSource.AUTOMATIC: (
                "Saves only YouTube's speech-recognition captions, up to "
                f"{MAX_SUBTITLE_LANGUAGES} languages. These exist for almost every video, but "
                "names, slang, and accents are often mistranscribed."
            ),
            SubtitleSource.ALL: (
                "Saves every caption track YouTube advertises, up to "
                f"{MAX_SUBTITLE_LANGUAGES} languages. Most videos offer well over a hundred "
                "machine-translated tracks, and YouTube starts refusing further caption "
                "requests partway through, so the request is capped."
            ),
        }[self]


class PlaylistMedia(str, Enum):
    """What a playlist download saves for each selected video."""

    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLES = "subtitles"

    @property
    def item_label(self) -> str:
        return {
            PlaylistMedia.VIDEO: "video",
            PlaylistMedia.AUDIO: "track",
            PlaylistMedia.SUBTITLES: "subtitle file",
        }[self]

    @property
    def format_label(self) -> str:
        return {
            PlaylistMedia.VIDEO: "MP4",
            PlaylistMedia.AUDIO: "MP3",
            PlaylistMedia.SUBTITLES: "SRT",
        }[self]

    @property
    def needs_quality(self) -> bool:
        return self is PlaylistMedia.VIDEO

    @property
    def needs_bitrate(self) -> bool:
        return self is PlaylistMedia.AUDIO


class SubtitleTrackInfo:
    """One caption track advertised for a video.

    Kept as a plain dataclass so the UI can render a language list without
    importing yt-dlp types.
    """

    __slots__ = ("language_code", "language_name", "automatic")

    def __init__(self, language_code: str, language_name: str, automatic: bool) -> None:
        self.language_code = language_code
        self.language_name = language_name
        self.automatic = automatic

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "auto" if self.automatic else "manual"
        return f"SubtitleTrackInfo({self.language_code!r}, {self.language_name!r}, {kind})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SubtitleTrackInfo):
            return NotImplemented
        return (
            self.language_code == other.language_code
            and self.language_name == other.language_name
            and self.automatic == other.automatic
        )

    def __hash__(self) -> int:
        return hash((self.language_code, self.language_name, self.automatic))

    @property
    def display_name(self) -> str:
        suffix = " (auto-generated)" if self.automatic else ""
        return f"{self.language_name} [{self.language_code}]{suffix}"


def estimate_audio_size_bytes(duration: float | None, bitrate: int | None) -> int | None:
    """Return a rough MP3 size for a duration and bitrate, or None when unknown."""

    if duration is None or duration <= 0 or bitrate is None or bitrate <= 0:
        return None
    try:
        seconds = float(duration)
        kilobits = float(bitrate)
    except (TypeError, ValueError):
        return None
    if seconds <= 0 or kilobits <= 0:
        return None
    return int(seconds * kilobits * 1000 / 8)


@dataclass(frozen=True)
class VideoCodecFamily(str, Enum):
    """Video codec families YouTube publishes, grouped by broad support.

    The app always writes MP4, so picking a codec is also a question of whether
    the stream can be placed in MP4 without a full re-encode.
    """

    H264 = "h264"
    VP9 = "vp9"
    AV1 = "av1"
    OTHER = "other"


class StreamPreference(str, Enum):
    """Which stream to pick when several share a resolution.

    ``QUALITY`` keeps the highest-bitrate stream, which is what YouTube's own
    player tends to serve.  ``SMALLER_FILE`` keeps the smallest stream that can
    still be written to MP4 without re-encoding, trading a little visible
    quality for a materially smaller download.
    """

    QUALITY = "quality"
    SMALLER_FILE = "smaller_file"

    @property
    def label(self) -> str:
        return "Best quality" if self is StreamPreference.QUALITY else "Smaller file"

    @property
    def tradeoff(self) -> str:
        if self is StreamPreference.QUALITY:
            return (
                "Picks the highest-bitrate stream for each resolution. Largest download, "
                "best picture quality."
            )
        return (
            "Picks the smallest stream that still fits MP4 without re-encoding. "
            "Much smaller download, slightly softer picture."
        )


def codec_family_of(vcodec: str | None) -> VideoCodecFamily:
    """Classify a yt-dlp ``vcodec`` value into a broad codec family."""

    value = (vcodec or "").strip().lower()
    if not value or value == "none":
        return VideoCodecFamily.OTHER
    if value.startswith(("avc", "h264")):
        return VideoCodecFamily.H264
    if value.startswith(("vp9", "vp09")):
        return VideoCodecFamily.VP9
    if value.startswith(("av01", "av1")):
        return VideoCodecFamily.AV1
    return VideoCodecFamily.OTHER


@dataclass(frozen=True)
class VideoQuality:
    format_id: str
    height: int
    fps: float | None
    has_audio: bool
    width: int | None = None
    extension: str | None = None
    estimated_size_bytes: int | None = None
    # Set when another stream at the same resolution and frame rate offers a
    # smaller file, so the UI can offer the choice and the engine can act on it.
    smaller_format_id: str | None = None
    smaller_size_bytes: int | None = None
    smaller_codec: str | None = None

    @property
    def size_label(self) -> str:
        if self.estimated_size_bytes is None or self.estimated_size_bytes <= 0:
            return "size unavailable"
        return f"~{format_size(self.estimated_size_bytes)}"

    @property
    def has_smaller_alternative(self) -> bool:
        """Whether a genuinely smaller no-re-encode stream exists here."""

        return bool(
            self.smaller_format_id
            and self.smaller_format_id != self.format_id
            and self.smaller_size_bytes
            and self.estimated_size_bytes
            and 0 < self.smaller_size_bytes < self.estimated_size_bytes
        )

    @property
    def smaller_size_label(self) -> str:
        if not self.smaller_size_bytes or self.smaller_size_bytes <= 0:
            return "size unavailable"
        return f"~{format_size(self.smaller_size_bytes)}"

    @property
    def resolution_label(self) -> str:
        frame_rate = ""
        if self.fps and self.fps > 0:
            rounded = round(self.fps)
            frame_rate = str(rounded) if abs(self.fps - rounded) < 0.05 else f"{self.fps:.1f}"
            frame_rate = f"{frame_rate}fps"
        return f"{self.height}p {frame_rate}".strip()

    @property
    def display_name(self) -> str:
        return f"{self.resolution_label} · {self.size_label}"

    @property
    def sort_key(self) -> tuple[int, float]:
        return self.height, self.fps or 0.0

    def for_preference(self, preference: StreamPreference) -> str:
        """Return the format id this preference should download."""

        if preference is StreamPreference.SMALLER_FILE and self.has_smaller_alternative:
            return str(self.smaller_format_id)
        return self.format_id

    def size_for_preference(self, preference: StreamPreference) -> int | None:
        if preference is StreamPreference.SMALLER_FILE and self.has_smaller_alternative:
            return self.smaller_size_bytes
        return self.estimated_size_bytes

    def size_label_for_preference(self, preference: StreamPreference) -> str:
        size = self.size_for_preference(preference)
        if size is None or size <= 0:
            return "size unavailable"
        return f"~{format_size(size)}"


@dataclass(frozen=True)
class PlaylistQuality:
    """A resolution/frame-rate choice shared by all videos in a playlist."""

    height: int
    fps: float | None
    estimated_size_bytes: int | None = None
    available_video_count: int = 0
    total_video_count: int = 0
    # Aggregate size of the smaller-file alternative across the same videos.
    smaller_size_bytes: int | None = None
    smaller_video_count: int = 0

    @property
    def has_smaller_alternative(self) -> bool:
        return bool(
            self.smaller_video_count > 0
            and self.smaller_size_bytes
            and self.estimated_size_bytes
            and 0 < self.smaller_size_bytes < self.estimated_size_bytes
        )

    @property
    def smaller_size_label(self) -> str:
        if not self.smaller_size_bytes or self.smaller_size_bytes <= 0:
            return "size unavailable"
        return f"~{format_size(self.smaller_size_bytes)}"

    @property
    def size_label(self) -> str:
        if self.estimated_size_bytes is None or self.estimated_size_bytes <= 0:
            return "size unavailable"
        return f"~{format_size(self.estimated_size_bytes)}"

    @property
    def resolution_label(self) -> str:
        """Just the resolution/frame-rate text, without any size suffix."""

        frame_rate = ""
        if self.fps and self.fps > 0:
            rounded = round(self.fps)
            frame_rate = str(rounded) if abs(self.fps - rounded) < 0.05 else f"{self.fps:.1f}"
            frame_rate = f"{frame_rate}fps"
        return f"{self.height}p {frame_rate}".strip()

    def size_for_preference(self, preference: StreamPreference) -> int | None:
        if preference is StreamPreference.SMALLER_FILE and self.has_smaller_alternative:
            return self.smaller_size_bytes
        return self.estimated_size_bytes

    def size_label_for_preference(self, preference: StreamPreference) -> str:
        size = self.size_for_preference(preference)
        if size is None or size <= 0:
            return "size unavailable"
        return f"~{format_size(size)}"

    @property
    def display_name(self) -> str:
        return f"{self.resolution_label} · {self.size_label}"

    @property
    def sort_key(self) -> tuple[int, float]:
        return self.height, self.fps or 0.0


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str
    duration: float | None
    thumbnail_url: str | None
    qualities: tuple[VideoQuality, ...]
    audio_available: bool
    is_live: bool = False
    normalized_url: str = ""
    # A playlist entry that is not selectable in the UI, e.g. an unavailable
    # or private video.  Its reason is shown in the playlist list.
    selectable: bool = True
    unavailable_reason: str = ""
    subtitles_available: bool = False
    subtitle_tracks: tuple[SubtitleTrackInfo, ...] = ()

    @property
    def manual_subtitle_tracks(self) -> tuple[SubtitleTrackInfo, ...]:
        return tuple(track for track in self.subtitle_tracks if not track.automatic)

    @property
    def automatic_subtitle_tracks(self) -> tuple[SubtitleTrackInfo, ...]:
        return tuple(track for track in self.subtitle_tracks if track.automatic)


@dataclass(frozen=True)
class PlaylistInfo:
    playlist_id: str
    title: str
    videos: tuple[VideoInfo, ...]
    qualities: tuple[PlaylistQuality, ...]
    normalized_url: str = ""
    skipped_count: int = 0

    @property
    def video_count(self) -> int:
        """Number of listed entries, including ones that cannot be downloaded."""

        return len(self.videos)

    @property
    def selectable_video_count(self) -> int:
        """Number of listed entries that can actually be downloaded."""

        return sum(1 for video in self.videos if video.selectable)

    @property
    def unavailable_video_count(self) -> int:
        return self.video_count - self.selectable_video_count


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    output_dir: Path
    mode: DownloadMode
    info: VideoInfo
    quality: VideoQuality | None = None
    audio_bitrate: int | None = None
    preference: StreamPreference = StreamPreference.QUALITY
    subtitle_format: SubtitleFormat = SubtitleFormat.SRT
    subtitle_source: SubtitleSource = SubtitleSource.PREFERRED
    subtitle_language: str = ""
    embed_cover: bool = True


@dataclass(frozen=True)
class PlaylistDownloadRequest:
    """One shared quality, media type, and selection for a playlist job.

    ``selected_video_ids`` limits the job to the videos chosen in the UI.
    ``None`` keeps every probed video, which is the default for callers that
    do not offer a per-video selection.
    """

    url: str
    output_dir: Path
    info: PlaylistInfo
    quality: PlaylistQuality | None = None
    media: PlaylistMedia = PlaylistMedia.VIDEO
    audio_bitrate: int | None = None
    selected_video_ids: tuple[str, ...] | None = None
    preference: StreamPreference = StreamPreference.QUALITY
    subtitle_format: SubtitleFormat = SubtitleFormat.SRT
    subtitle_source: SubtitleSource = SubtitleSource.PREFERRED
    embed_cover: bool = True


@dataclass(frozen=True)
class QueueItemResult:
    """Outcome for one link in a batch queue."""

    url: str
    index: int
    path: Path | None = None
    error: str = ""
    title: str = ""
    used_fallback_quality: bool = False


@dataclass(frozen=True)
class QueueDownloadRequest:
    """Several independent links saved one after another.

    Every link is probed and downloaded in turn.  A failure on one link does
    not stop the rest, matching how playlist downloads already behave.
    """

    urls: tuple[str, ...]
    output_dir: Path
    media: DownloadMode = DownloadMode.VIDEO
    quality: PlaylistQuality | None = None
    audio_bitrate: int | None = None
    preference: StreamPreference = StreamPreference.QUALITY
    subtitle_format: SubtitleFormat = SubtitleFormat.SRT
    subtitle_source: SubtitleSource = SubtitleSource.PREFERRED
    embed_cover: bool = True

    @property
    def total(self) -> int:
        return len(self.urls)

    @property
    def needs_quality(self) -> bool:
        return self.media is DownloadMode.VIDEO

    @property
    def needs_bitrate(self) -> bool:
        return self.media is DownloadMode.AUDIO


@dataclass(frozen=True)
class QueueDownloadResult:
    items: tuple[QueueItemResult, ...]
    media: DownloadMode = DownloadMode.VIDEO

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def saved(self) -> tuple[QueueItemResult, ...]:
        return tuple(item for item in self.items if item.path is not None)

    @property
    def failures(self) -> tuple[QueueItemResult, ...]:
        return tuple(item for item in self.items if item.path is None)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self.saved if item.path is not None)

    @property
    def fallback_count(self) -> int:
        return sum(1 for item in self.items if item.used_fallback_quality)


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    percent: float | None
    message: str
    speed: str | None = None
    eta: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    mode: DownloadMode
    subtitle_paths: tuple[Path, ...] = ()
    # Set when a job stopped early but usable files were still written, so the
    # window can show both the files and what went wrong.
    warning: str = ""


@dataclass(frozen=True)
class PlaylistDownloadResult:
    paths: tuple[Path, ...]
    total: int
    failures: tuple[tuple[str, str], ...] = ()
    failed_video_ids: tuple[str, ...] = ()
    fallback_video_count: int = 0
    media: PlaylistMedia = PlaylistMedia.VIDEO
    skipped_count: int = 0

    @property
    def success_count(self) -> int:
        return len(self.paths)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def item_label(self) -> str:
        return self.media.item_label


ProgressCallback = Callable[[ProgressEvent], None]
CancelCheck = Callable[[], bool]
