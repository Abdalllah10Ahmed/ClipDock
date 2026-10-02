from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from youtube_downloader.core.engine import (
    CLEAN_OUTTMPL,
    ID_OUTTMPL,
    Engine,
    _ProgressRelay,
    _retry_backoff,
    item_slice_progress,
    playlist_audio_size_bytes,
    playlist_video_size_bytes,
    quality_matches_target,
    select_playlist_video_quality,
    select_playlist_videos,
)
from youtube_downloader.core.errors import CancelledError, DownloadFailure, ExtractionError, friendly_error
from youtube_downloader.core.models import (
    MAX_SUBTITLE_LANGUAGES,
    DownloadMode,
    DownloadRequest,
    PlaylistDownloadRequest,
    PlaylistMedia,
    PlaylistQuality,
    QueueDownloadRequest,
    StreamPreference,
    SubtitleFormat,
    SubtitleSource,
    VideoInfo,
    VideoQuality,
)


# Long enough to clear the engine's 0.10s progress throttle, so simulated hook
# events are not all collapsed into the first one.
_PROGRESS_TICK = 0.12


def expand_template(template: str, title: str, video_id: str, extension: str) -> str:
    """Expand the slice of yt-dlp's output template the fakes actually use.

    The fakes used to write a hardcoded ``Title [id].ext`` and ignore the
    template entirely, which is why no test noticed when the naming changed.
    Expanding the real template keeps them honest: if the engine hands over a
    different one, the file these fakes create moves with it.
    """

    name = template
    for field, value in (
        ("%(title).180B", title),
        ("%(title)s", title),
        ("%(id)s", video_id),
        ("%(ext)s", extension.lstrip(".")),
    ):
        name = name.replace(field, value)
    return name


class FakeYdl:
    instances: list["FakeYdl"] = []
    # Caption tracks advertised by the fake video.  A test sets this to {} to
    # simulate a video that has no captions at all.
    caption_tracks: dict[str, dict] = {
        "subtitles": {
            "en": [{"name": "English"}],
            "fr": [{"name": "French"}],
        },
        "automatic_captions": {
            "en": [{"name": "English (auto-generated)"}],
            "de": [{"name": "German (auto-generated)"}],
        },
    }
    # When positive, a caption run writes this many languages and then fails,
    # which is what YouTube does once a multi-language run trips its rate limit.
    fail_after_subtitles = 0

    def __init__(self, options: dict) -> None:
        self.options = options
        self.downloaded = False
        self._extra_postprocessors: list[Any] = []
        # Minimal attributes the real YoutubeDL has and postprocessors expect.
        self.params = options
        self._postprocessor_hooks: list[Any] = []

        def _stub(*args: Any, **kwargs: Any) -> None:
            pass

        self.report_warning = _stub
        self.report_error = _stub
        self.to_screen = _stub
        self.write_debug = _stub
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeYdl":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def add_post_processor(self, pp: Any) -> None:
        # Mirror the real YoutubeDL API: the caller passes an already-constructed
        # postprocessor instance.  The fake just records it; the tests that
        # exercise the engine's wiring check the options dict and the existence
        # of the call rather than running the PP.
        self._extra_postprocessors.append(pp)

    def extract_info(self, url: str, download: bool = False):
        if not download:
            data = {
                "_type": "video",
                "id": "video123",
                "title": "Example video",
                "duration": 42,
                "thumbnails": [
                    {"url": "https://img.example/small.jpg", "width": 320, "height": 180, "preference": 1},
                    {"url": "https://img.example/large.jpg", "width": 1280, "height": 720, "preference": 2},
                ],
                "formats": [
                    {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 2_000_000},
                    {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "width": 1280, "fps": 30, "tbr": 2500, "filesize": 10_000_000},
                    {"format_id": "v720-muxed", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "width": 1280, "fps": 30, "tbr": 2400, "filesize": 12_000_000},
                    {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 5000, "filesize_approx": 20_000_000},
                ],
            }
            data.update(self.__class__.caption_tracks)
            return data
        self.downloaded = True
        template = self.options["outtmpl"]["default"]
        base = Path(expand_template(template, "Example video", "video123", "mp4")).with_suffix("")
        if self.options.get("skip_download"):
            # A caption run writes one file per language and saves no media.
            # Real yt-dlp reports these under requested_subtitles, not in
            # requested_downloads, so the fake matches that shape exactly.
            extension = self.options["subtitlesformat"]
            written = {}
            for index, language in enumerate(self.options["subtitleslangs"]):
                if self.fail_after_subtitles and index >= self.fail_after_subtitles:
                    # YouTube stops answering partway through a long run.
                    raise RuntimeError("the caption endpoint stopped responding")
                output = base.with_name(f"{base.name}.{language}.{extension}")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
                written[language] = {"ext": extension, "filepath": str(output)}
            return {
                "_type": "video",
                "id": "video123",
                "title": "Example video",
                "requested_subtitles": written,
            }
        if self.options.get("merge_output_format") == "mp4":
            output = base.with_suffix(".mp4")
        else:
            output = base.with_suffix(".mp3")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"test-media")
        return {
            "_type": "video",
            "id": "video123",
            "title": "Example video",
            "requested_downloads": [{"filepath": str(output)}],
        }


class FakePlaylistYdl:
    instances: list["FakePlaylistYdl"] = []
    download_order: list[str] = []
    failed_ids: set[str] = {"bad"}
    # Percentages fed through the progress hooks on each download.  The default
    # is empty so unrelated tests stay fast and deterministic.
    progress_percentages: tuple[float, ...] = ()

    def __init__(self, options: dict) -> None:
        self.options = options
        # Minimal attributes the real YoutubeDL has and postprocessors expect.
        self.params = options
        self._postprocessor_hooks: list[Any] = []

        def _stub(*args: Any, **kwargs: Any) -> None:
            pass

        self.report_warning = _stub
        self.report_error = _stub
        self.to_screen = _stub
        self.write_debug = _stub
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakePlaylistYdl":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def add_post_processor(self, pp: Any) -> None:
        # Same as FakeYdl: record the postprocessor for tests that check it.
        pass

    @staticmethod
    def _video_data(video_id: str) -> dict:
        if video_id == "v1":
            return {
                "_type": "video",
                "id": video_id,
                "title": "First video",
                "duration": 20,
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "formats": [
                    {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 2_000_000},
                    {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 5000, "filesize": 10_000_000},
                    {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "width": 1280, "fps": 30, "tbr": 2500, "filesize": 5_000_000},
                ],
            }
        if video_id == "v2":
            return {
                "_type": "video",
                "id": video_id,
                "title": "Second video",
                "duration": 30,
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "formats": [
                    {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 1_000_000},
                    {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "width": 1280, "fps": 30, "tbr": 2200, "filesize": 4_000_000},
                ],
            }
        return {
            "_type": "video",
            "id": "bad",
            "title": "Unavailable video",
            "duration": 10,
            "webpage_url": "https://www.youtube.com/watch?v=bad",
            "formats": [
                {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 500_000},
                {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 4000, "filesize": 8_000_000},
                {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720, "width": 1280, "fps": 30, "tbr": 2000, "filesize": 3_000_000},
            ],
        }

    def _emit_progress(self) -> None:
        """Drive the progress hooks the way a merged download would.

        A video and its audio arrive as two separate files, so yt-dlp reports a
        fresh 0-100% for each.  The payload mirrors a real hook event, byte
        counts included, because that is all the engine's hook reads.  The pause
        matters too: the engine throttles hook events, and without real time
        passing between them every event after the first is dropped and the
        reset never reaches the progress bar.
        """

        for percent in self.progress_percentages:
            event = {
                "status": "downloading",
                "downloaded_bytes": percent,
                "total_bytes": 100,
            }
            for hook in self.options.get("progress_hooks") or []:
                hook(event)
            time.sleep(_PROGRESS_TICK)

    def extract_info(self, url: str, download: bool = False):
        parsed = urlsplit(url)
        if not download and parsed.path.rstrip("/") == "/playlist":
            return {
                "_type": "playlist",
                "id": "PL123",
                "title": "Example playlist",
                "entries": [self._video_data("v1"), self._video_data("v2"), None, self._video_data("bad")],
            }
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if not video_id and parsed.netloc.endswith("youtu.be"):
            # Short links carry the id in the path rather than the query.
            video_id = parsed.path.strip("/").split("/")[0]
        if not video_id:
            raise RuntimeError("missing video id")
        if download and video_id in self.failed_ids:
            self.download_order.append(video_id)
            raise RuntimeError("simulated per-video failure")
        data = self._video_data(video_id)
        if not download:
            return data
        self.download_order.append(video_id)
        self._emit_progress()
        template = self.options["outtmpl"]["default"]
        wants_audio = any(
            isinstance(item, dict) and item.get("key") == "FFmpegExtractAudio"
            for item in (self.options.get("postprocessors") or [])
        )
        extension = ".mp3" if wants_audio else ".mp4"
        output = Path(expand_template(template, data["title"], video_id, extension))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"playlist media")
        return {
            "_type": "video",
            "id": video_id,
            "title": data["title"],
            "requested_downloads": [{"filepath": str(output)}],
        }


class FakeStreamYdl:
    """A video whose streams show why a smaller-file option is needed.

    The 1080p group holds a bloated HLS variant, a lean progressive MP4, and a
    WebM/VP9 stream.  The 2160p group holds a huge HLS variant and a VP9 stream
    with no MP4 counterpart at all, which is the case that must not offer a
    smaller file because saving it would mean re-encoding.
    """

    instances: list["FakeStreamYdl"] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeStreamYdl":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def extract_info(self, url: str, download: bool = False):
        if not download:
            return {
                "_type": "video",
                "id": "streams",
                "title": "Stream comparison",
                "duration": 100,
                "formats": [
                    {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 1_000_000},
                    {"format_id": "hls-1080", "ext": "m3u8", "vcodec": "avc1", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 5000, "filesize": 60_000_000},
                    {"format_id": "prog-1080", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 2500, "filesize": 25_000_000},
                    {"format_id": "vp9-1080", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 1080, "width": 1920, "fps": 60, "tbr": 1200, "filesize": 12_000_000},
                    {"format_id": "hls-2160", "ext": "m3u8", "vcodec": "avc1", "acodec": "none", "height": 2160, "width": 3840, "fps": 60, "tbr": 12000, "filesize": 400_000_000},
                    {"format_id": "vp9-2160", "ext": "webm", "vcodec": "vp09.00.50.08", "acodec": "none", "height": 2160, "width": 3840, "fps": 60, "tbr": 3000, "filesize": 80_000_000},
                ],
            }
        self.downloaded = True
        template = self.options["outtmpl"]["default"]
        base = Path(template).parent / "Stream comparison [streams]"
        output = base.with_suffix(".mp4")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"stream media")
        return {
            "_type": "video",
            "id": "streams",
            "title": "Stream comparison",
            "requested_downloads": [{"filepath": str(output)}],
        }


class FilenameTests(unittest.TestCase):
    """Clean filenames by default, the video id only where one is actually needed.

    The collision half of this needs a real ``YoutubeDL``, because the engine
    deliberately asks yt-dlp for the filename rather than predicting it -- yt-dlp
    owns the Windows sanitisation, the reserved device names, and the 180
    character trim, and the fakes elsewhere in this file cannot answer that
    question.  The other half is asserted through the fakes, which expand the
    real template, so a change to the template moves the file they create.
    """

    def setUp(self) -> None:
        FakeYdl.instances.clear()
        FakeYdl.fail_after_subtitles = 0
        FakePlaylistYdl.instances.clear()
        FakePlaylistYdl.download_order.clear()
        self.engine = Engine(ydl_factory=FakeYdl, ffmpeg_path=Path("ffmpeg.exe"))
        self.real_engine = Engine(ffmpeg_path=Path("ffmpeg.exe"))

    @staticmethod
    def _request(directory: str, video_id: str, title: str, mode: DownloadMode) -> DownloadRequest:
        return DownloadRequest(
            url=f"https://youtu.be/{video_id}",
            output_dir=Path(directory),
            mode=mode,
            info=VideoInfo(
                video_id=video_id,
                title=title,
                duration=100.0,
                thumbnail_url=None,
                qualities=(),
                audio_available=True,
            ),
        )

    def _taken(self, directory: str, video_id: str, title: str, extension: str, mode: DownloadMode) -> bool:
        """Ask the real check, through a real YoutubeDL built as the engine builds one."""

        request = self._request(directory, video_id, title, mode)
        options = self.real_engine._base_options(Path(directory), _ProgressRelay(None, None))
        options["paths"] = {"home": str(Path(directory)), "temp": str(Path(directory) / ".parts")}
        ydl = self.real_engine._new_ydl(options)
        return self.real_engine._clean_name_is_taken(ydl, request, extension)

    def test_a_free_name_is_not_reported_as_taken(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(self._taken(directory, "aaa", "Alan Walker - Faded", ".mp4", DownloadMode.VIDEO))

    def test_the_same_title_under_a_different_id_is_reported_as_taken(self) -> None:
        # The case the id exists for: a playlist of videos all called
        # "Official Video", where the second would otherwise be reported as
        # already downloaded and quietly never saved.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Alan Walker - Faded.mp4").write_bytes(b"x")
            self.assertTrue(self._taken(directory, "bbb", "Alan Walker - Faded", ".mp4", DownloadMode.VIDEO))

    def test_a_different_title_is_unaffected_by_a_taken_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Alan Walker - Faded.mp4").write_bytes(b"x")
            self.assertFalse(self._taken(directory, "ccc", "Alan Walker - Ignite", ".mp4", DownloadMode.VIDEO))

    def test_a_caption_file_is_found_despite_its_language_code(self) -> None:
        # A caption file is "Title.en.srt", so checking only for "Title.srt"
        # would miss it and two same-titled videos would overwrite each other.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Alan Walker - Faded.en.srt").write_text("x", encoding="utf-8")
            self.assertTrue(self._taken(directory, "ddd", "Alan Walker - Faded", ".srt", DownloadMode.SUBTITLES))

    def test_a_media_download_is_not_confused_by_a_caption_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Alan Walker - Faded.en.srt").write_text("x", encoding="utf-8")
            self.assertFalse(self._taken(directory, "eee", "Alan Walker - Faded", ".mp4", DownloadMode.VIDEO))

    def test_an_empty_title_never_claims_a_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".srt").write_text("x", encoding="utf-8")
            self.assertFalse(self._taken(directory, "fff", "", ".srt", DownloadMode.SUBTITLES))

    def test_the_download_actually_writes_the_clean_name(self) -> None:
        # The end-to-end version: a real download through the fakes, asserting on
        # the file that appears rather than on the options that were passed.
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_video(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.VIDEO,
                    info=info,
                    quality=info.qualities[0],
                )
            )
            self.assertEqual(result.path.name, "Example video.mp4")
            self.assertNotIn("[video123]", result.path.name)

    def test_the_caption_rescue_ignores_files_a_previous_run_left_behind(self) -> None:
        # The rescue used to find files by searching for the video id, which
        # matched any caption file for that video however old -- so a repeat
        # failure would report the *first* run's files as this run's work.
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Example video.en.srt").write_text("old", encoding="utf-8")
            request = self._request(directory, "video123", "Example video", DownloadMode.SUBTITLES)

            # A snapshot taken now sees the stale file, so it is not reported.
            self.assertEqual(
                self.engine._caption_files_written_since(
                    request, SubtitleFormat.SRT.extension, self.engine._folder_snapshot(Path(directory))
                ),
                (),
            )

            # A snapshot taken before that file existed reports it, and reports
            # only what appeared afterwards.  Compared by name because
            # _prepare_output_dir resolves the path, and on this machine that
            # turns the long folder name into its 8.3 short form.
            fresh = Path(directory) / "Example video.fr.srt"
            fresh.write_text("new", encoding="utf-8")
            found = self.engine._caption_files_written_since(
                request, SubtitleFormat.SRT.extension, frozenset({"Example video.en.srt"})
            )
            self.assertEqual([path.name for path in found], ["Example video.fr.srt"])

    def test_both_templates_are_written_down_once(self) -> None:
        # They used to be spelled out in two places and drifted; a third copy in
        # the caption rescue is what broke silently.  This pins that there is one
        # clean template and one fallback, and that the fallback differs only by
        # the id.
        import inspect

        source = inspect.getsource(Engine)
        self.assertEqual(source.count('"%(title).180B.%(ext)s"') + source.count('"%(title).180B [%(id)s].%(ext)s"'), 0,
                         "a template literal is back inside engine.py")
        self.assertEqual(CLEAN_OUTTMPL, "%(title).180B.%(ext)s")
        self.assertEqual(ID_OUTTMPL, "%(title).180B [%(id)s].%(ext)s")


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeYdl.instances.clear()
        FakeYdl.fail_after_subtitles = 0
        FakeYdl.caption_tracks = {
            "subtitles": {"en": [{"name": "English"}], "fr": [{"name": "French"}]},
            "automatic_captions": {
                "en": [{"name": "English (auto-generated)"}],
                "de": [{"name": "German (auto-generated)"}],
            },
        }
        FakePlaylistYdl.instances.clear()
        FakePlaylistYdl.download_order.clear()
        FakePlaylistYdl.failed_ids = {"bad"}
        FakePlaylistYdl.progress_percentages = ()
        FakeStreamYdl.instances.clear()
        self.engine = Engine(ydl_factory=FakeYdl, ffmpeg_path=Path("ffmpeg.exe"))
        self.playlist_engine = Engine(ydl_factory=FakePlaylistYdl, ffmpeg_path=Path("ffmpeg.exe"))
        self.stream_engine = Engine(ydl_factory=FakeStreamYdl, ffmpeg_path=Path("ffmpeg.exe"))
        self.queue_engine = Engine(ydl_factory=FakePlaylistYdl, ffmpeg_path=Path("ffmpeg.exe"))

    def test_probe_returns_unique_video_qualities_and_largest_thumbnail(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        self.assertEqual(info.video_id, "video123")
        self.assertEqual([(item.height, item.fps) for item in info.qualities], [(1080, 60), (720, 30)])
        self.assertEqual(info.qualities[1].format_id, "v720")
        self.assertEqual(info.qualities[0].estimated_size_bytes, 22_000_000)
        self.assertEqual(info.qualities[1].estimated_size_bytes, 12_000_000)
        self.assertIn("21 MB", info.qualities[0].display_name)
        self.assertEqual(info.thumbnail_url, "https://img.example/large.jpg")
        self.assertTrue(info.audio_available)

    def test_playlist_probe_builds_shared_quality_choices_and_skips_unusable_entries(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(info.playlist_id, "PL123")
        self.assertEqual(info.title, "Example playlist")
        self.assertEqual(info.video_count, 3)
        self.assertEqual(info.skipped_count, 1)
        self.assertEqual(
            [(quality.height, quality.fps) for quality in info.qualities],
            [(1080, 60), (720, 30)],
        )
        top_quality = info.qualities[0]
        self.assertEqual(top_quality.available_video_count, 2)
        self.assertEqual(top_quality.total_video_count, 3)
        self.assertIsNone(top_quality.estimated_size_bytes)
        lower_quality = info.qualities[1]
        self.assertEqual(lower_quality.available_video_count, 3)
        self.assertEqual(lower_quality.estimated_size_bytes, 15_500_000)
        self.assertFalse(FakePlaylistYdl.instances[-1].options["noplaylist"])

    def test_download_dispatches_explicit_playlist_request(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.playlist_engine.download(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    quality=info.qualities[0],
                )
            )
        self.assertEqual(result.success_count, 2)
        self.assertEqual(result.failed_count, 1)

        with tempfile.TemporaryDirectory() as directory:
            mode_result = self.playlist_engine.download(
                DownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    mode=DownloadMode.PLAYLIST,
                    info=info,
                    quality=info.qualities[0],
                )
            )
        self.assertEqual(mode_result.success_count, 2)

    def test_playlist_download_is_sequential_continues_after_failure_and_reports_progress(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        target = info.qualities[0]
        events = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    quality=target,
                ),
                progress=events.append,
            )
            self.assertEqual(result.success_count, 2)
            self.assertEqual(result.failed_count, 1)
            self.assertEqual(result.fallback_video_count, 1)
            self.assertEqual(FakePlaylistYdl.download_order, ["v1", "v2", "bad"])
            self.assertEqual(len(result.paths), 2)
            self.assertTrue(all(path.is_file() for path in result.paths))
        self.assertTrue(events)
        self.assertEqual(events[-1].percent, 100.0)
        self.assertTrue(all(event.percent is not None for event in events))
        percentages = [float(event.percent) for event in events if event.percent is not None]
        self.assertEqual(percentages, sorted(percentages))
        self.assertTrue(any("Video 2 of 3" in event.message for event in events))
        download_instances = [
            instance
            for instance in FakePlaylistYdl.instances
            if instance.options.get("merge_output_format") == "mp4"
        ]
        self.assertEqual(len(download_instances), 3)
        self.assertTrue(all(instance.options["noplaylist"] for instance in download_instances))

    def test_progress_relay_throttles_rapid_chunks_and_keeps_forced_events(self) -> None:
        events = []
        relay = _ProgressRelay(events.append, lambda: False)
        with patch("youtube_downloader.core.engine.time.monotonic", return_value=10.0):
            for _ in range(100):
                relay.emit("downloading", None, "Downloading media")
        self.assertEqual(len(events), 1)
        relay.emit("completed", 100.0, "Download complete", force=True)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].percent, 100.0)

    def test_video_download_selects_requested_quality_and_outputs_mp4(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_video(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.VIDEO,
                    info=info,
                    quality=info.qualities[0],
                )
            )
            self.assertEqual(result.path.suffix, ".mp4")
            self.assertTrue(result.path.is_file())
            self.assertGreater(result.path.stat().st_size, 0)
        options = FakeYdl.instances[-1].options
        self.assertIn("v1080+bestaudio", options["format"])
        self.assertEqual(options["merge_output_format"], "mp4")
        self.assertEqual(options["postprocessors"][0]["key"], "FFmpegVideoConvertor")
        self.assertEqual(
            options["postprocessor_args"],
            {"videoconvertor+ffmpeg": ["-c:v", "libopenh264", "-c:a", "aac"]},
        )

    def test_audio_download_sets_requested_mp3_bitrate(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_audio(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.AUDIO,
                    info=info,
                    audio_bitrate=192,
                )
            )
            self.assertEqual(result.path.suffix, ".mp3")
        options = FakeYdl.instances[-1].options
        self.assertEqual(options["postprocessors"][0]["preferredquality"], "192")
        self.assertEqual(options["postprocessors"][0]["preferredcodec"], "mp3")

    def test_audio_download_embeds_the_cover_in_the_mp3(self) -> None:
        # A track with no artwork shows a blank icon in every player, so the
        # thumbnail is fetched and embedded.  The fetch only makes sense as part
        # of this, and the embed has to come after the conversion, or it would
        # attach to the intermediate m4a instead of the MP3.
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            self.engine.download_audio(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.AUDIO,
                    info=info,
                    audio_bitrate=192,
                )
            )
        options = FakeYdl.instances[-1].options
        self.assertTrue(options["writethumbnail"])
        self.assertEqual(
            [item["key"] for item in options["postprocessors"]],
            ["FFmpegExtractAudio"],
        )
        # The tolerant embed thumbnail is added as an instance after the
        # options-level postprocessors, so it does not appear in the options
        # dictionary but runs last, on the converted MP3.

    def test_only_the_audio_path_asks_for_a_thumbnail(self) -> None:
        # The video path has no use for a loose image and would leave one beside
        # every download, so the flag has to stay scoped to audio.
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            self.engine.download_video(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.VIDEO,
                    info=info,
                    quality=info.qualities[0],
                )
            )
        self.assertNotIn("writethumbnail", FakeYdl.instances[-1].options)

    def test_thumbnail_download_uses_largest_image_only(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")

        class OneChunkResponse:
            headers = {"Content-Length": "8", "Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size: int) -> bytes:
                if hasattr(self, "done"):
                    return b""
                self.done = True
                return b"\xff\xd8\xffdata"

        with tempfile.TemporaryDirectory() as directory, patch("youtube_downloader.core.engine.urllib.request.urlopen", return_value=OneChunkResponse()):
            result = self.engine.download_thumbnail(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.THUMBNAIL,
                    info=info,
                )
            )
            self.assertEqual(result.path.suffix, ".jpg")
            self.assertEqual(result.path.read_bytes(), b"\xff\xd8\xffdata")
            self.assertEqual(FakeYdl.instances[-1].downloaded, False)

    def test_cancelled_operation_stops_before_download(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CancelledError):
                self.engine.download_audio(
                    DownloadRequest(
                        url=info.normalized_url,
                        output_dir=Path(directory),
                        mode=DownloadMode.AUDIO,
                        info=info,
                        audio_bitrate=128,
                    ),
                    cancel_check=lambda: True,
                )

    def test_download_rejects_collection_urls_at_the_engine_boundary(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ExtractionError):
            self.engine.download(
                DownloadRequest(
                    url="https://youtube.com/playlist?list=abc",
                    output_dir=Path(directory),
                    mode=DownloadMode.THUMBNAIL,
                    info=info,
                )
            )

    def test_playlist_download_reports_failure_when_every_item_fails(self) -> None:
        FakePlaylistYdl.failed_ids = {"v1", "v2", "bad"}
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure) as context:
                self.playlist_engine.download_playlist(
                    PlaylistDownloadRequest(
                        url="https://www.youtube.com/playlist?list=PL123",
                        output_dir=Path(directory),
                        info=info,
                        quality=info.qualities[0],
                    )
                )
        self.assertEqual(context.exception.code, "playlist_failed")
        self.assertIn("First item:", context.exception.message)

    def test_playlist_download_only_processes_selected_videos(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    quality=info.qualities[1],
                    selected_video_ids=("v2",),
                )
            )
        self.assertEqual(result.total, 1)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(FakePlaylistYdl.download_order, ["v2"])
        self.assertEqual(result.skipped_count, 2)

    def test_playlist_download_requires_at_least_one_selected_video(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure) as context:
            self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    quality=info.qualities[0],
                    selected_video_ids=(),
                )
            )
        self.assertEqual(context.exception.code, "no_selection")

    def test_playlist_audio_download_saves_mp3_with_shared_bitrate(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        events = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    media=PlaylistMedia.AUDIO,
                    audio_bitrate=192,
                    selected_video_ids=("v1", "v2"),
                ),
                progress=events.append,
            )
        self.assertIs(result.media, PlaylistMedia.AUDIO)
        self.assertEqual(result.total, 2)
        self.assertEqual(result.success_count, 2)
        self.assertTrue(all(path.suffix == ".mp3" for path in result.paths))
        self.assertEqual(FakePlaylistYdl.download_order, ["v1", "v2"])
        audio_instances = [
            instance
            for instance in FakePlaylistYdl.instances
            if any(
                isinstance(item, dict) and item.get("key") == "FFmpegExtractAudio"
                for item in (instance.options.get("postprocessors") or [])
            )
        ]
        self.assertEqual(len(audio_instances), 2)
        self.assertTrue(
            all(
                instance.options["postprocessors"][0]["preferredquality"] == "192"
                for instance in audio_instances
            )
        )
        self.assertTrue(events)
        self.assertEqual(events[-1].percent, 100.0)
        self.assertIn("2 of 2 tracks saved", events[-1].message)

    def test_playlist_audio_requires_a_supported_bitrate(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure) as context:
            self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    media=PlaylistMedia.AUDIO,
                    audio_bitrate=999,
                )
            )
        self.assertEqual(context.exception.code, "bitrate_missing")

    def test_playlist_size_helpers_report_per_video_estimates(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        first, second, _third = info.videos
        top = info.qualities[0]
        # 1080p60 only exists on the first video, so the second falls back to
        # its 720p30 stream (4 MB video + 1 MB audio).
        self.assertEqual(playlist_video_size_bytes(first, top), 12_000_000)
        self.assertEqual(playlist_video_size_bytes(second, top), 5_000_000)
        self.assertFalse(quality_matches_target(select_playlist_video_quality(second, top), top))
        # A video with no audio cannot produce a complete MP4 or an MP3.
        silent = VideoInfo(
            "silent",
            "Silent video",
            30,
            None,
            (VideoQuality("v1080", 1080, 60, False, 1920, "mp4", 9_000_000),),
            False,
        )
        self.assertIsNone(playlist_video_size_bytes(silent, top))
        self.assertIsNone(playlist_audio_size_bytes(silent, 320))
        # 20s and 30s at 192 kbps.
        self.assertEqual(playlist_audio_size_bytes(first, 192), 480_000)
        self.assertEqual(playlist_audio_size_bytes(second, 192), 720_000)
        self.assertIsNone(playlist_audio_size_bytes(first, None))

    def test_select_playlist_videos_preserves_order_and_defaults_to_all(self) -> None:
        videos = (
            VideoInfo("a", "A", 10, None, (), True),
            VideoInfo("b", "B", 10, None, (), True),
            VideoInfo("c", "C", 10, None, (), True),
        )
        self.assertEqual(len(select_playlist_videos(videos, None)), 3)
        self.assertEqual(
            [video.video_id for video in select_playlist_videos(videos, ("c", "a"))],
            ["a", "c"],
        )
        self.assertEqual(select_playlist_videos(videos, ()), [])

    def test_smaller_file_preference_offers_the_lean_mp4_stream(self) -> None:
        info = self.stream_engine.probe("https://youtu.be/streams")
        by_height = {quality.height: quality for quality in info.qualities}

        # 1080p: the best pick is the bloated HLS variant, and the smaller-file
        # alternative is the lean progressive MP4 at the same resolution.  The
        # reported sizes include the best audio track both variants would merge.
        top = by_height[1080]
        self.assertEqual(top.format_id, "hls-1080")
        self.assertEqual(top.smaller_format_id, "prog-1080")
        self.assertEqual(top.smaller_codec, "h264")
        self.assertEqual(top.smaller_size_bytes, 26_000_000)
        self.assertEqual(top.for_preference(StreamPreference.QUALITY), "hls-1080")
        self.assertEqual(top.for_preference(StreamPreference.SMALLER_FILE), "prog-1080")
        self.assertEqual(top.size_for_preference(StreamPreference.QUALITY), top.estimated_size_bytes)
        self.assertEqual(top.size_for_preference(StreamPreference.SMALLER_FILE), 26_000_000)

        # 2160p: the only smaller stream is WebM/VP9, which would need a
        # re-encode, so no alternative is offered and the smaller-file
        # preference falls back to the ordinary stream and size.
        ultra = by_height[2160]
        self.assertIsNone(ultra.smaller_format_id)
        self.assertFalse(ultra.has_smaller_alternative)
        self.assertEqual(ultra.for_preference(StreamPreference.SMALLER_FILE), "hls-2160")
        self.assertEqual(
            ultra.size_for_preference(StreamPreference.SMALLER_FILE),
            ultra.estimated_size_bytes,
        )

    def test_video_download_uses_the_selected_stream_preference(self) -> None:
        info = self.stream_engine.probe("https://youtu.be/streams")
        quality = next(item for item in info.qualities if item.height == 1080)
        with tempfile.TemporaryDirectory() as directory:
            self.stream_engine.download_video(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.VIDEO,
                    info=info,
                    quality=quality,
                    preference=StreamPreference.SMALLER_FILE,
                )
            )
        self.assertEqual(FakeStreamYdl.instances[-1].options["format"], "prog-1080+bestaudio[ext=m4a]/prog-1080+bestaudio")

    def test_playlist_qualities_report_a_smaller_total_only_when_every_video_has_one(self) -> None:
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        # The fake playlist offers one stream per height, so no video has a
        # smaller alternative and the totals must not claim one.
        for quality in info.qualities:
            self.assertIsNone(quality.smaller_size_bytes)
            self.assertEqual(quality.smaller_video_count, 0)

    def test_subtitle_download_writes_only_the_requested_caption_files(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        self.assertTrue(info.subtitles_available)
        self.assertEqual(
            {track.language_code for track in info.manual_subtitle_tracks},
            {"en", "fr"},
        )
        self.assertEqual(
            {track.language_code for track in info.automatic_subtitle_tracks},
            {"en", "de"},
        )
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_format=SubtitleFormat.SRT,
                    subtitle_source=SubtitleSource.PREFERRED,
                )
            )
        self.assertEqual([path.suffix for path in result.subtitle_paths], [".srt", ".srt"])
        self.assertEqual(result.path, result.subtitle_paths[0])
        options = FakeYdl.instances[-1].options
        # A caption run must not also download the media.
        self.assertTrue(options["skip_download"])
        self.assertTrue(options["writesubtitles"])
        # PREFERRED only asks for author-written tracks, so the automatic
        # fallback flag stays off.
        self.assertFalse(options["writeautomaticsub"])
        self.assertEqual(options["subtitlesformat"], "srt")
        self.assertEqual(options["subtitleslangs"], ["en", "fr"])
        self.assertEqual(options["postprocessors"], [])

    def test_subtitle_source_selects_tracks_and_enables_the_automatic_fallback(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            automatic = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_source=SubtitleSource.AUTOMATIC,
                )
            )
        options = FakeYdl.instances[-1].options
        self.assertEqual(options["subtitleslangs"], ["de", "en"])
        self.assertTrue(options["writeautomaticsub"])
        # yt-dlp hands back the author-written track whenever writesubtitles is
        # on, so an ASR-only request has to turn it off or it is not ASR at all.
        self.assertFalse(options["writesubtitles"])
        self.assertEqual(len(automatic.subtitle_paths), 2)

        with tempfile.TemporaryDirectory() as directory:
            every = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_format=SubtitleFormat.VTT,
                    subtitle_source=SubtitleSource.ALL,
                )
            )
        options = FakeYdl.instances[-1].options
        # ALL asks for concrete codes rather than an uncapped "everything",
        # so the engine's own limit applies.
        self.assertEqual(options["subtitleslangs"], ["de", "en", "fr"])
        self.assertEqual(options["subtitlesformat"], "vtt")
        # A multi-language run is paced so the caption endpoint keeps answering.
        self.assertGreater(options["sleep_interval_subtitles"], 0)
        self.assertEqual(len(every.subtitle_paths), 3)
        self.assertTrue(all(path.suffix == ".vtt" for path in every.subtitle_paths))
        self.assertEqual(every.warning, "")

    def test_a_single_caption_request_is_not_delayed(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_language="en",
                )
            )
        self.assertEqual(FakeYdl.instances[-1].options["subtitleslangs"], ["en"])
        self.assertEqual(FakeYdl.instances[-1].options["sleep_interval_subtitles"], 0)

    def test_a_refused_request_is_retried_with_a_delay(self) -> None:
        # Ten retries with no pause cannot outlast a rate limit, so the engine
        # supplies its own backoff.
        self.assertGreater(_retry_backoff(0), 0.0)
        self.assertGreater(_retry_backoff(3), _retry_backoff(0))
        self.assertLessEqual(_retry_backoff(20), 31.0)

    def test_all_tracks_caps_the_language_request(self) -> None:
        # A real video advertises well over a hundred machine-translated
        # tracks, and fetching them all trips YouTube's caption rate limit.
        languages = [f"xx{index}" for index in range(MAX_SUBTITLE_LANGUAGES + 40)]
        FakeYdl.caption_tracks = {
            "subtitles": {"en": [{"name": "English"}]},
            "automatic_captions": {code: [{"name": code}] for code in languages},
        }
        info = self.engine.probe("https://youtu.be/video123")
        self.assertGreater(len(info.subtitle_tracks), MAX_SUBTITLE_LANGUAGES)
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_source=SubtitleSource.ALL,
                )
            )
        requested = FakeYdl.instances[-1].options["subtitleslangs"]
        self.assertEqual(len(requested), MAX_SUBTITLE_LANGUAGES)
        self.assertIn("en", requested)
        self.assertEqual(len(result.subtitle_paths), MAX_SUBTITLE_LANGUAGES)

    def test_the_author_fallback_is_capped_too(self) -> None:
        # A video with no author-written captions falls back to the automatic
        # list, which is just as long and just as rate-limited, so the cap has
        # to apply on that path as well.
        languages = [f"xx{index}" for index in range(MAX_SUBTITLE_LANGUAGES + 40)]
        FakeYdl.caption_tracks = {
            "subtitles": {},
            "automatic_captions": {code: [{"name": code}] for code in languages},
        }
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                )
            )
        self.assertEqual(
            len(FakeYdl.instances[-1].options["subtitleslangs"]), MAX_SUBTITLE_LANGUAGES
        )

    def test_asr_only_runs_are_capped(self) -> None:
        languages = [f"xx{index}" for index in range(MAX_SUBTITLE_LANGUAGES + 40)]
        FakeYdl.caption_tracks = {
            "subtitles": {"en": [{"name": "English"}]},
            "automatic_captions": {code: [{"name": code}] for code in languages},
        }
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_source=SubtitleSource.AUTOMATIC,
                )
            )
        self.assertEqual(
            len(FakeYdl.instances[-1].options["subtitleslangs"]), MAX_SUBTITLE_LANGUAGES
        )

    def test_a_caption_run_cut_short_still_reports_the_files_it_wrote(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        FakeYdl.fail_after_subtitles = 2
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_source=SubtitleSource.ALL,
                )
            )
            self.assertEqual(len(result.subtitle_paths), 2)
            self.assertTrue(all(path.is_file() for path in result.subtitle_paths))
        self.assertIn("2 of 3", result.warning)

    def test_a_caption_run_that_wrote_nothing_still_fails(self) -> None:
        FakeYdl.caption_tracks = {}
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure):
            self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                )
            )

    def test_subtitle_download_falls_back_to_automatic_tracks_without_author_captions(self) -> None:
        FakeYdl.caption_tracks = {"automatic_captions": {"es": [{"name": "Spanish (auto-generated)"}]}}
        info = self.engine.probe("https://youtu.be/video123")
        self.assertEqual(info.manual_subtitle_tracks, ())
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                    subtitle_source=SubtitleSource.PREFERRED,
                )
            )
        self.assertEqual(FakeYdl.instances[-1].options["subtitleslangs"], ["es"])
        self.assertEqual(len(result.subtitle_paths), 1)

    def test_subtitle_download_reports_when_no_captions_are_advertised(self) -> None:
        FakeYdl.caption_tracks = {}
        info = self.engine.probe("https://youtu.be/video123")
        self.assertFalse(info.subtitles_available)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure) as context:
            self.engine.download_subtitles(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                )
            )
        self.assertEqual(context.exception.code, "subtitles_missing")

    def test_queue_downloads_links_in_order_and_continues_past_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.queue_engine.download_queue(
                QueueDownloadRequest(
                    urls=(
                        "https://youtu.be/v1",
                        "https://youtu.be/bad",
                        "https://youtu.be/v2",
                    ),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                )
            )
        # The links are handled in the order they were pasted, and the failing
        # one does not stop the last.
        self.assertEqual(FakePlaylistYdl.download_order, ["v1", "bad", "v2"])
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.saved), 2)
        self.assertEqual(len(result.failures), 1)
        # v1 has a real 1080p stream; v2 tops out at 720p, so it is a fallback.
        self.assertFalse(result.items[0].used_fallback_quality)
        self.assertTrue(result.items[2].used_fallback_quality)
        self.assertEqual(result.fallback_count, 1)
        # A failed link carries a readable reason instead of a path.
        self.assertIsNone(result.failures[0].path)
        self.assertTrue(result.failures[0].error)
        self.assertTrue(all(path.suffix == ".mp4" for path in result.paths))
        self.assertEqual(result.media, DownloadMode.VIDEO)

    def test_a_finished_probe_does_not_consume_the_items_whole_slice(self) -> None:
        # Reading details finishes long before the file does.  If its 100% ate
        # the whole slice, the bar would jump to the end of the item and then
        # sit frozen for the entire download.
        FakePlaylistYdl.progress_percentages = (0, 50, 100)
        percents: list[float] = []
        media_percents: list[float] = []

        def record(event) -> None:
            if event.percent is None:
                return
            percents.append(event.percent)
            if "Downloading" in event.message:
                media_percents.append(event.percent)

        with tempfile.TemporaryDirectory() as directory:
            self.queue_engine.download_queue(
                QueueDownloadRequest(
                    urls=("https://youtu.be/v1",),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                ),
                progress=record,
            )
        # One link, so the whole job is that link's slice.
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents[-1], 100.0)
        # The details finished at a quarter of the slice, so the download has
        # somewhere left to go and the bar keeps moving.
        probe_end = item_slice_progress(0, 1, 100.0, probing=True)
        self.assertLess(probe_end, 50.0)
        self.assertTrue(any(probe_end < value < 100.0 for value in media_percents))

    def test_item_slice_progress_splits_details_from_media(self) -> None:
        first = item_slice_progress(0, 2, 100.0, probing=True)
        last = item_slice_progress(0, 2, 100.0, probing=False)
        second_start = item_slice_progress(1, 2, 0.0, probing=True)
        self.assertAlmostEqual(first, 7.5)
        self.assertAlmostEqual(last, 50.0)
        self.assertAlmostEqual(second_start, 50.0)
        self.assertLess(first, last)

    def test_queue_progress_never_walks_backwards(self) -> None:
        # yt-dlp reports a fresh 0-100% for each file of a merged download, so
        # a link whose video stream finishes and whose audio stream then starts
        # would drag the bar backwards without the clamp.
        FakePlaylistYdl.progress_percentages = (0, 50, 100, 0, 40, 100)
        percents: list[float] = []

        def record(event) -> None:
            if event.percent is not None:
                percents.append(event.percent)

        with tempfile.TemporaryDirectory() as directory:
            self.queue_engine.download_queue(
                QueueDownloadRequest(
                    urls=("https://youtu.be/v1", "https://youtu.be/v2"),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                ),
                progress=record,
            )
        self.assertTrue(percents)
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents[-1], 100.0)

    def test_playlist_progress_never_walks_backwards(self) -> None:
        # The same clamp guards playlist downloads, which share the maths.
        FakePlaylistYdl.progress_percentages = (0, 60, 100, 0, 30, 100)
        info = self.playlist_engine.probe_playlist("https://www.youtube.com/playlist?list=PL123")
        percents: list[float] = []

        def record(event) -> None:
            if event.percent is not None:
                percents.append(event.percent)

        with tempfile.TemporaryDirectory() as directory:
            self.playlist_engine.download_playlist(
                PlaylistDownloadRequest(
                    url="https://www.youtube.com/playlist?list=PL123",
                    output_dir=Path(directory),
                    info=info,
                    media=PlaylistMedia.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                ),
                progress=record,
            )
        self.assertTrue(percents)
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents[-1], 100.0)

    def test_queue_download_reports_failure_when_every_link_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure) as context:
            self.queue_engine.download_queue(
                QueueDownloadRequest(
                    urls=("https://youtu.be/bad",),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                )
            )
        self.assertEqual(context.exception.code, "queue_failed")

    def test_queue_download_requires_a_quality_and_a_supported_bitrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure) as missing_quality:
                self.queue_engine.download_queue(
                    QueueDownloadRequest(
                        urls=("https://youtu.be/v1",),
                        output_dir=Path(directory),
                        media=DownloadMode.VIDEO,
                    )
                )
            self.assertEqual(missing_quality.exception.code, "quality_missing")
            with self.assertRaises(DownloadFailure) as missing_bitrate:
                self.queue_engine.download_queue(
                    QueueDownloadRequest(
                        urls=("https://youtu.be/v1",),
                        output_dir=Path(directory),
                        media=DownloadMode.AUDIO,
                        audio_bitrate=111,
                    )
                )
            self.assertEqual(missing_bitrate.exception.code, "bitrate_missing")

    def test_queue_rejects_an_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DownloadFailure) as context:
            self.queue_engine.download_queue(
                QueueDownloadRequest(urls=(), output_dir=Path(directory), media=DownloadMode.VIDEO)
            )
        self.assertEqual(context.exception.code, "queue_empty")

    def test_queue_cancellation_stops_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(CancelledError):
            self.queue_engine.download_queue(
                QueueDownloadRequest(
                    urls=("https://youtu.be/v1", "https://youtu.be/v2"),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                ),
                cancel_check=lambda: True,
            )

    def test_download_dispatches_subtitle_and_queue_requests(self) -> None:
        info = self.engine.probe("https://youtu.be/video123")
        with tempfile.TemporaryDirectory() as directory:
            result = self.engine.download(
                DownloadRequest(
                    url=info.normalized_url,
                    output_dir=Path(directory),
                    mode=DownloadMode.SUBTITLES,
                    info=info,
                )
            )
        self.assertEqual(result.path.suffix, ".srt")
        with tempfile.TemporaryDirectory() as directory:
            queued = self.queue_engine.download(
                QueueDownloadRequest(
                    urls=("https://youtu.be/v1",),
                    output_dir=Path(directory),
                    media=DownloadMode.VIDEO,
                    quality=PlaylistQuality(height=1080, fps=None),
                )
            )
        self.assertEqual(len(queued.saved), 1)

    def test_javascript_challenge_error_is_explicit_without_adding_dependencies(self) -> None:
        mapped = friendly_error(RuntimeError("JsChallengeProviderRejectedRequest: remote component unavailable"))
        self.assertEqual(mapped.code, "missing_dependency")
        self.assertIn("yt-dlp-ejs", mapped.message)

    def test_friendly_error_explains_network_and_storage_failures(self) -> None:
        network = friendly_error(RuntimeError("HTTP Error 503: Service Unavailable"))
        self.assertEqual(network.code, "network_failure")
        storage = friendly_error(OSError(28, "No space left on device"))
        self.assertEqual(storage.code, "storage_error")
        self.assertIn("drive", storage.message.lower())


if __name__ == "__main__":
    unittest.main()
