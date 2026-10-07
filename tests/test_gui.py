from __future__ import annotations

import dataclasses
import json
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPalette, QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QPushButton

from youtube_downloader.core import history
from youtube_downloader.core.binaries import find_icon, require_ffmpeg
from youtube_downloader.core.errors import CancelledError, DependencyError
from youtube_downloader.core.updates import RELEASES_URL, UpdateCheck

from youtube_downloader.core.models import (
    DownloadMode,
    DownloadResult,
    PlaylistDownloadResult,
    PlaylistInfo,
    PlaylistMedia,
    PlaylistQuality,
    ProgressEvent,
    QueueDownloadRequest,
    QueueDownloadResult,
    QueueItemResult,
    StreamPreference,
    SubtitleFormat,
    SubtitleSource,
    SubtitleTrackInfo,
    VideoInfo,
    VideoQuality,
    format_size,
)
from youtube_downloader.gui import main_window
from youtube_downloader.gui import caption
from youtube_downloader.gui.caption import CaptionButton
from youtube_downloader.gui.main_window import (
    APP_NAME,
    PLAYLIST_COLUMN_CHECK,
    PLAYLIST_COLUMN_SIZE,
    QUEUE_COLUMN_SIZE,
    QUEUE_COLUMN_STATUS,
    QUEUE_COLUMN_TITLE,
    THEME_SETTING_KEY,
    TITLE_BAR_HEIGHT,
    MainWindow,
)
from youtube_downloader.gui.selectors import SelectorComboBox
from youtube_downloader.gui.themes import DEFAULT_THEME, THEMES, theme_palette
from youtube_downloader.gui.workers import JobWorker

# How far, per colour channel, a painted pixel may sit from the exact blend of a
# backdrop and the colour it was drawn in, and still count as an antialiased
# version of that colour.  Generous, because the rounding in an antialiased edge
# is genuinely lossy - but two orders of magnitude below the 32-67 that a mark
# painted in a third colour deviates by, so nothing is hiding in between.
_BLEND_TOLERANCE = 6


class FakeEngine:
    def probe(self, url: str, *, progress=None, cancel_check=None):
        # Carries a smaller H.264 MP4 at the same resolution and frame rate,
        # which is what a real probe reports for most 720p videos.
        quality = VideoQuality(
            "v720", 720, 30, True, 1280, "mp4", 12_000_000,
            smaller_format_id="v720-lean",
            smaller_size_bytes=7_000_000,
            smaller_codec="avc1.4d401e",
        )
        return VideoInfo(
            "id",
            "A test video",
            10,
            "https://img.example/max.jpg",
            (quality,),
            True,
            False,
            url,
            subtitles_available=True,
            subtitle_tracks=(
                SubtitleTrackInfo("en", "English", False),
                SubtitleTrackInfo("de", "German (auto-generated)", True),
            ),
        )

    def download(self, request, *, progress=None, cancel_check=None):
        if isinstance(request, QueueDownloadRequest):
            return self._download_queue(request, progress=progress)
        if request.mode is DownloadMode.SUBTITLES:
            caption = request.output_dir / "downloaded.en.srt"
            caption.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
            return DownloadResult(caption, request.mode, (caption,))
        output = request.output_dir / "downloaded.mp4"
        output.write_bytes(b"test media")
        return DownloadResult(output, request.mode)

    def _download_queue(self, request, *, progress=None):
        items = []
        for position, url in enumerate(request.urls, start=1):
            if progress:
                progress(ProgressEvent("queue", position / request.total * 100, f"Link {position}"))
            if "bad" in url:
                items.append(
                    QueueItemResult(url=url, index=position, error="This link could not be read.")
                )
                continue
            output = request.output_dir / f"queued-{position}.mp4"
            output.write_bytes(b"queued media")
            items.append(QueueItemResult(url=url, index=position, path=output, title=f"Video {position}"))
        if progress:
            progress(ProgressEvent("completed", 100.0, "Batch complete"))
        return QueueDownloadResult(items=tuple(items), media=request.media)

    def probe_playlist(self, url: str, *, progress=None, cancel_check=None):
        # The 1080p choice is the one with a leaner alternative across its
        # videos, so it is the one the smaller-file preference stays enabled for.
        top = PlaylistQuality(
            1080, 60, 24_000_000, 2, 3,
            smaller_size_bytes=14_000_000,
            smaller_video_count=2,
        )
        lower = PlaylistQuality(720, 30, 21_000_000, 3, 3)
        # The winning 1080p stream carries a leaner H.264 alternative, as a real
        # probe reports, so a per-row size can change with the preference.
        stream = VideoQuality(
            "v1080", 1080, 60, True, 1920, "mp4", 12_000_000,
            smaller_format_id="v1080-lean",
            smaller_size_bytes=7_000_000,
            smaller_codec="avc1.4d401e",
        )
        videos = (
            VideoInfo("playlist-video-1", "Playlist video 1", 10, None, (stream,), True, False, "https://youtu.be/playlist-video-1"),
            VideoInfo("playlist-video-2", "Playlist video 2", 12, None, (stream,), True, False, "https://youtu.be/playlist-video-2"),
            VideoInfo(
                "playlist-video-3",
                "Removed video",
                None,
                None,
                (),
                False,
                False,
                "https://youtu.be/playlist-video-3",
                False,
                "This video is no longer available",
            ),
        )
        return PlaylistInfo("PL123", "A playlist", videos, (top, lower), url, skipped_count=1)

    def download_playlist(self, request, *, progress=None, cancel_check=None):
        media = request.media
        extension = ".mp4" if media is PlaylistMedia.VIDEO else ".mp3"
        selected = request.selected_video_ids
        videos = [
            video
            for video in request.info.videos
            if video.selectable and (selected is None or video.video_id in selected)
        ]
        paths = []
        for index, video in enumerate(videos, start=1):
            output = request.output_dir / f"playlist-{video.video_id}{extension}"
            output.write_bytes(b"playlist media")
            paths.append(output)
        if progress:
            progress(ProgressEvent("completed", 100.0, "Playlist complete"))
        return PlaylistDownloadResult(tuple(paths), len(paths), media=media)


class ProgressReportingEngine:
    """Wraps an engine so every operation reports progress the way the real one does.

    `FakeEngine` returns instantly without emitting, which is right for testing
    what the window does with a result and wrong for testing progress: with no
    events there is no transition to watch.  This adds the events, paced with a
    short sleep so the bar genuinely has to move between them.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.downloaded = False

    def _paced(self, progress: object, percentages: tuple[float, ...], message: str) -> None:
        if progress is None:
            return
        for percent in percentages:
            progress(ProgressEvent("downloading", percent, message))
            QTest.qWait(15)

    def probe(self, url: str, *, progress=None, cancel_check=None):
        self._paced(progress, (5.0, 40.0, 100.0), "Reading details")
        return self._inner.probe(url, progress=progress, cancel_check=cancel_check)

    def download(self, request: object, *, progress=None, cancel_check=None):
        self.downloaded = True
        self._paced(progress, (0.0, 8.0, 12.0, 47.0, 93.0), "Downloading media")
        self._paced(progress, (100.0,), "Downloading media")
        return self._inner.download(request, progress=progress, cancel_check=cancel_check)


class PausingEngine:
    """A download that writes a partial file and then stops only when told to.

    `FakeEngine.download` returns immediately, so there is no moment at which a
    pause could be pressed and nothing for it to act on.  This one writes the
    piece, then waits on `cancel_check` the way the real engine waits between
    fragments, and raises `CancelledError` once the flag is set -- which is
    exactly the shape the real worker sees, so the window's half is exercised
    rather than assumed.

    ``writes`` picks what the stopped run leaves behind, because each of the
    three shapes has to be told apart by the button: a real ``.part``, only the
    ``.ytdl`` state file that means a run started but got nowhere, and nothing
    at all.

    ``stops`` is how many runs stop before one is allowed to finish, so a test
    can watch Resume re-run the job and see it complete instead of hanging on a
    second stop nobody asked for.
    """

    def __init__(self, *, writes: str = "part", stops: int = 1) -> None:
        if writes not in {"part", "state", "nothing"}:
            raise ValueError(writes)
        self.writes = writes
        self.stops = stops
        self.downloads_started = 0

    def probe(self, url: str, *, progress=None, cancel_check=None):
        return FakeEngine().probe(url, progress=progress, cancel_check=cancel_check)

    def _run(self, output_dir: Path, progress) -> bool:
        """Write this run's pieces; return False when the run may finish."""
        self.downloads_started += 1
        if self.downloads_started > self.stops:
            return False
        if self.writes == "part":
            (output_dir / "half-done.mp4.part").write_bytes(b"x" * 2048)
        elif self.writes == "state":
            (output_dir / "half-done.mp4.ytdl").write_bytes(b"{}")
        if progress:
            progress(ProgressEvent("downloading", 3.0, "Downloading media"))
        return True

    def _stop_when_asked(self, cancel_check) -> None:
        while not cancel_check():
            time.sleep(0.005)
        raise CancelledError()

    def download(self, request, *, progress=None, cancel_check=None):
        if self._run(request.output_dir, progress):
            self._stop_when_asked(cancel_check)
        (request.output_dir / "half-done.mp4.part").unlink(missing_ok=True)
        output = request.output_dir / "half-done.mp4"
        output.write_bytes(b"finished media")
        return DownloadResult(output, request.mode)

    def download_playlist(self, request, *, progress=None, cancel_check=None):
        if self._run(request.output_dir, progress):
            self._stop_when_asked(cancel_check)
        raise AssertionError("a playlist run is never completed by this fake")

    def download_queue(self, request, *, progress=None, cancel_check=None):
        if self._run(request.output_dir, progress):
            self._stop_when_asked(cancel_check)
        raise AssertionError("a queue run is never completed by this fake")


class SmallerFileExplanationTests(unittest.TestCase):
    """The absence of an option has to be explained, and the explanation has to be true.

    These need no window and no QApplication: `_why_no_smaller_file` is a static
    method, so there is nothing to skip if Qt is unavailable. That matters here
    because the defect being pinned was a *false* sentence, and a test that
    quietly skips is a test that would have let it through.

    The sentence this replaces claimed the refused streams "are WebM, and
    converting those would mean re-encoding". Measured on Sprite Fright, the
    refused stream at 858p is format 400: `ext=mp4`, `av01`, 104 MB. It is an MP4
    file, it needs no re-encoding, and it was refused for its codec alone.
    """

    def test_the_refused_codec_and_size_are_named(self) -> None:
        refused = 114_000_000
        quality = VideoQuality(
            "hls-858", 858, 24, False, 1542, "mp4", 351_000_000,
            unoffered_smaller_size_bytes=refused,
            unoffered_smaller_codec="av1",
        )
        message = MainWindow._why_no_smaller_file(quality)

        # The codec the engine recorded has to be the codec the user is told.
        self.assertIn("AV1", message)
        # And the size, through the same formatter the rest of the app uses, so
        # the sentence cannot drift from the model it describes. Comparing against
        # `format_size` rather than a literal like "114 MB" is deliberate: those
        # units are binary, so 114_000_000 is 108.7 MB, and hardcoding the wrong
        # arithmetic in the test is how this assertion came to fail first.
        self.assertIn(format_size(refused), message)
        # The saving is the point of mentioning it at all.
        self.assertIn("H.264", message)

    def test_it_never_claims_the_refused_streams_are_webm(self) -> None:
        """The specific falsehood, pinned so it cannot come back.

        Every real video measured serves the refused alternatives in MP4
        containers, so "WebM" was never true of anything a user could have
        downloaded. Asserting its absence directly is stronger than asserting
        the presence of the right wording, which could be reworded later.
        """

        for codec in ("av1", "vp9", "h264"):
            quality = VideoQuality(
                f"hls-{codec}", 1080, 60, False, 1920, "mp4", 60_000_000,
                unoffered_smaller_size_bytes=26_000_000,
                unoffered_smaller_codec=codec,
            )
            message = MainWindow._why_no_smaller_file(quality)
            self.assertNotIn("WebM", message)
            self.assertNotIn("webm", message)
            # "re-encoding" is the other half of the false claim: these streams
            # need none, which is precisely why the wording was wrong.
            self.assertNotIn("re-encod", message)

    def test_vp9_is_named_as_vp9(self) -> None:
        refused = 23_000_000
        quality = VideoQuality(
            "hls-720", 720, 24, False, 1280, "mp4", 51_000_000,
            unoffered_smaller_size_bytes=refused,
            unoffered_smaller_codec="vp9",
        )
        message = MainWindow._why_no_smaller_file(quality)
        self.assertIn("VP9", message)
        self.assertIn(format_size(refused), message)

    def test_nothing_smaller_at_all_says_only_that(self) -> None:
        """When there is genuinely nothing, it must not invent something.

        The engine leaves both fields None when no smaller stream exists at all,
        so the message has to fall back to the plain truth. Asserting it names
        no codec stops a stale or defaulted value from leaking into the text.
        """

        quality = VideoQuality("only-1080", 1080, 60, False, 1920, "mp4", 60_000_000)
        self.assertFalse(quality.has_unoffered_smaller_alternative)
        message = MainWindow._why_no_smaller_file(quality)

        self.assertIn("only one H.264", message)
        for codec_name in ("AV1", "VP9", "WebM"):
            self.assertNotIn(codec_name, message)

    def test_a_zero_size_does_not_count_as_a_refusal(self) -> None:
        """A recorded size of zero is no saving at all, so it must not be shown.

        The model treats zero as absent, and the explanation has to agree with
        the model or the two drift: a tooltip claiming a smaller stream exists
        when nothing does is the same class of defect as the one being fixed.
        """

        quality = VideoQuality(
            "hls-1080", 1080, 60, False, 1920, "mp4", 60_000_000,
            unoffered_smaller_size_bytes=0,
            unoffered_smaller_codec="av1",
        )
        self.assertFalse(quality.has_unoffered_smaller_alternative)
        self.assertIn("only one H.264", MainWindow._why_no_smaller_file(quality))

    def test_an_unknown_codec_does_not_render_as_the_word_none(self) -> None:
        """A codec the app does not recognise must still read as English.

        The failure mode this guards is the obvious one: interpolating a raw or
        defaulted value into a sentence that already names a codec. The first
        draft fell back to "another codec" and produced "YouTube has a smaller
        another codec stream here", which this test caught. An unrecognised codec
        should now drop the word entirely rather than supply a bad one.
        """

        refused = 26_000_000
        quality = VideoQuality(
            "hls-1080", 1080, 60, False, 1920, "mp4", 60_000_000,
            unoffered_smaller_size_bytes=refused,
            unoffered_smaller_codec=None,
        )
        message = MainWindow._why_no_smaller_file(quality)
        self.assertIn(format_size(refused), message)
        self.assertNotIn("None", message)
        # No stray word between "smaller" and "stream" either.
        self.assertIn("a smaller stream here", message)


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # Every test in this class builds a real window, and building one reads
        # the saved theme while changing one writes it.  Pointed at a per-test
        # temporary file, so running the suite can never change the theme the
        # developer sees on their own machine - which is exactly what happened
        # before this existed.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = Path(directory.name) / "settings.json"
        original_read = main_window.read_settings
        original_write = main_window.write_setting
        main_window.read_settings = lambda: original_read(settings_path)
        main_window.write_setting = lambda key, value: original_write(
            key, value, settings_path
        )
        self.addCleanup(setattr, main_window, "read_settings", original_read)
        self.addCleanup(setattr, main_window, "write_setting", original_write)
        self.settings_path = settings_path
        # The same for the history.  It is reached the other way round - the
        # window does not hold the path, it calls `record_job`, which asks the
        # history module where it lives - so the module attribute itself is
        # swapped rather than the name imported into `main_window`.  Swapping
        # the name there would leave `read_history` reading the developer's own
        # file while the test wrote to a different one, and a test that passes
        # while the two disagree is worse than no test at all.
        history_path = Path(directory.name) / "history.json"
        original_history = history.default_history_path
        history.default_history_path = lambda: history_path
        self.addCleanup(setattr, history, "default_history_path", original_history)
        self.history_path = history_path

    def test_window_constructs_without_network(self) -> None:
        window = MainWindow(FakeEngine())
        self.assertEqual(window.windowTitle(), APP_NAME)
        self.assertFalse(window.download_button.isEnabled())
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self.assertTrue(window.controller.is_running)
        for _ in range(100):
            QTest.qWait(50)
            self.app.processEvents()
            if not window.controller.is_running:
                break
        self.assertFalse(window.controller.is_running)
        QTest.qWait(10)
        self.app.processEvents()
        self.assertEqual(window.quality_combo.count(), 1)
        self.assertIn("MB", window.quality_combo.itemText(0))
        # The size lives in the quality list, so there is no separate estimate.
        self.assertNotIn("MB", window.summary_label.text())
        window.mode_combo.setCurrentIndex(1)
        self.assertIn("~", window.bitrate_combo.itemText(0))
        self.assertFalse(window.summary_label.isVisibleTo(window))
        self.assertTrue(window.download_button.isEnabled())
        window.close()
        self.app.processEvents()

    def test_playlist_mode_lists_videos_with_sizes_and_downloads_selection(self) -> None:
        window = MainWindow(FakeEngine())
        playlist_index = window.mode_combo.findData("playlist")
        self.assertGreaterEqual(playlist_index, 0)
        window.mode_combo.setCurrentIndex(playlist_index)
        self.assertTrue(window.playlist_group.isVisibleTo(window))
        window.url_edit.setText("https://www.youtube.com/playlist?list=PL123")
        window._fetch_details()
        self._wait_for_job(window)
        self.assertIsNotNone(window.playlist_info)
        self.assertEqual(window.quality_combo.count(), 2)
        self.assertIn("1080p 60fps", window.quality_combo.itemText(0))
        self.assertIn("3 videos", window.title_label.text())
        self.assertEqual(window.playlist_table.rowCount(), 3)
        self.assertEqual(
            window.playlist_table.horizontalHeaderItem(3).text(),
            "Size at MP4",
        )
        # The removed entry is listed but cannot be selected.
        removed_row = 2
        removed_check = window.playlist_table.item(removed_row, PLAYLIST_COLUMN_CHECK)
        self.assertEqual(removed_check.checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(removed_check.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        # Each selectable row shows its own size for the chosen quality.
        top_row_size = window.playlist_table.item(0, PLAYLIST_COLUMN_SIZE).text()
        self.assertIn("MB", top_row_size)
        self.assertIn("2 of 3 videos selected", window.playlist_summary_label.text())
        # Per-video sizes are the table's own job; the summary line adds nothing.
        self.assertNotIn("~", window.summary_label.text())
        self.assertTrue(window.download_button.isEnabled())

        # Deselecting the first video updates the totals and the download.
        window.playlist_table.item(0, PLAYLIST_COLUMN_CHECK).setCheckState(Qt.CheckState.Unchecked)
        self.app.processEvents()
        self.assertIn("1 of 3 videos selected", window.playlist_summary_label.text())
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self.assertTrue(window.controller.is_running)
            self._wait_for_job(window)
            self.assertIn("1 of 1 videos saved as MP4", window.status_label.text())
            self.assertTrue((Path(directory) / "playlist-playlist-video-2.mp4").is_file())
            self.assertFalse((Path(directory) / "playlist-playlist-video-1.mp4").exists())
        window.close()
        self.app.processEvents()

    def test_playlist_audio_mode_switches_sizes_and_saves_mp3(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("playlist"))
        window.url_edit.setText("https://www.youtube.com/playlist?list=PL123")
        window._fetch_details()
        self._wait_for_job(window)
        self.assertIsNotNone(window.playlist_info)

        audio_index = window.playlist_media_combo.findData(PlaylistMedia.AUDIO.value)
        self.assertGreaterEqual(audio_index, 0)
        window.playlist_media_combo.setCurrentIndex(audio_index)
        self.app.processEvents()
        self.assertEqual(
            window.playlist_table.horizontalHeaderItem(3).text(),
            "Size at MP3",
        )
        # 10s and 12s at 128 kbps are ~160 KB and ~192 KB.
        self.assertIn("KB", window.playlist_table.item(0, PLAYLIST_COLUMN_SIZE).text())
        self.assertEqual(window.bitrate_combo.currentData(), 128)
        # The bitrate drives the per-video size.
        window.bitrate_combo.setCurrentIndex(window.bitrate_combo.findData(320))
        self.app.processEvents()
        self.assertIn("KB", window.playlist_table.item(0, PLAYLIST_COLUMN_SIZE).text())

        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self._wait_for_job(window)
            self.assertIn("2 of 2 tracks saved as MP3", window.status_label.text())
            self.assertTrue((Path(directory) / "playlist-playlist-video-1.mp3").is_file())
            self.assertTrue((Path(directory) / "playlist-playlist-video-2.mp3").is_file())
            # The unavailable entry is never downloaded, in either media mode.
            self.assertFalse((Path(directory) / "playlist-playlist-video-3.mp3").exists())
        window.close()
        self.app.processEvents()

    def test_playlist_clear_and_select_all_buttons_control_the_selection(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("playlist"))
        window.url_edit.setText("https://www.youtube.com/playlist?list=PL123")
        window._fetch_details()
        self._wait_for_job(window)

        window.select_none_button.click()
        self.app.processEvents()
        self.assertIn("No videos selected", window.playlist_summary_label.text())
        self.assertFalse(window.download_button.isEnabled())

        window.select_all_button.click()
        self.app.processEvents()
        self.assertIn("2 of 3 videos selected", window.playlist_summary_label.text())
        self.assertTrue(window.download_button.isEnabled())
        window.close()
        self.app.processEvents()

    def test_theme_control_does_not_stretch_when_window_is_wide(self) -> None:
        window = MainWindow(FakeEngine())
        window.resize(1600, 900)
        window.show()
        self.app.processEvents()
        label_width = window.theme_label.width()
        combo_width = window.theme_combo.width()
        container_width = window.theme_container.width()
        self.assertLessEqual(container_width, label_width + combo_width + 40)
        self.assertEqual(window.theme_label.sizePolicy().horizontalPolicy().name, "Fixed")
        self.assertEqual(window.theme_combo.sizePolicy().horizontalPolicy().name, "Fixed")
        # The container stays compact instead of expanding with the window.
        self.assertLess(container_width, window.width() // 2)
        self.assertIn("Theme", window.theme_label.text())
        window.close()
        self.app.processEvents()

    def test_small_initial_window_keeps_status_text_readable_and_scrollable(self) -> None:
        window = MainWindow(FakeEngine())
        window.resize(760, 540)
        window.show()
        self.app.processEvents()
        self.assertGreaterEqual(window.title_label.height(), 24)
        self.assertGreaterEqual(window.status_label.height(), 24)
        self.assertGreaterEqual(window.summary_label.height(), 20)
        scroll_area = window.centralWidget().findChild(QScrollArea)
        self.assertIsNotNone(scroll_area)
        self.assertGreaterEqual(scroll_area.verticalScrollBar().maximum(), 0)
        window.close()
        self.app.processEvents()

    def test_theme_selector_switches_to_readable_dark_palette(self) -> None:
        window = MainWindow(FakeEngine())
        self.assertEqual(window.theme_combo.currentData(), "light")
        self.assertEqual(window.theme_label.text(), "Theme")
        self.assertIn("themeLabel", window.styleSheet())
        self.assertFalse(hasattr(window, "theme_hint"))
        light_text = window.palette().color(QPalette.ColorRole.WindowText).name().lower()

        window.theme_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(window.theme_combo.currentData(), "dark")
        self.assertEqual(window._theme, "dark")
        dark_text = window.palette().color(QPalette.ColorRole.WindowText).name().lower()
        self.assertNotEqual(light_text, dark_text)
        # Dark is VS Code Dark+, so the editor's own greys are what the window
        # is painted with, rather than the neutral slate it used to be.
        self.assertIn("#cccccc", window.styleSheet())
        self.assertIn("#1f1f1f", window.styleSheet())

        window.theme_combo.setCurrentIndex(0)
        self.assertEqual(window.theme_combo.currentData(), "light")
        window.close()
        self.app.processEvents()

    def test_progress_display_does_not_jump_backward_on_retries(self) -> None:
        window = MainWindow(FakeEngine())
        window._on_progress(ProgressEvent("downloading", 42.0, "Downloading media"))
        window._on_progress(ProgressEvent("processing", None, "Processing media"))
        window._on_progress(ProgressEvent("downloading", 17.0, "Downloading media"))
        self.assertEqual(window.progress_bar.value(), 42)
        window.close()
        self.app.processEvents()

    def test_a_leftover_event_from_the_finished_probe_does_not_pin_the_download(self) -> None:
        # The reported symptom: after *Fetch details* the bar sat at 100% and
        # stayed there for the whole download.  Two operations drive one bar, and
        # the probe's last value outliving its own operation was clamping every
        # value of the download that followed.
        window = MainWindow(FakeEngine())

        # Operation one: reading details, which legitimately ends at 100%.
        window._reset_progress_display()
        window._on_progress(ProgressEvent("downloading", 100.0, "Details read"))
        self.assertEqual(window.progress_bar.value(), 100)

        # Operation two starts, so the bar is cleared and belongs to the download.
        window._reset_progress_display()
        self.assertEqual(window.progress_bar.maximum(), 0, "the new operation should show an indeterminate bar")

        # A progress event from operation one arrives after operation two began.
        # It goes through the controller, which is the only place that still
        # knows which worker is current.
        finished = JobWorker(lambda emit, cancel: None)
        finished.progress.connect(
            lambda event, owner=finished: window.controller._relay_progress(owner, event)
        )
        window.controller._worker = JobWorker(lambda emit, cancel: None)
        finished._emit_progress(ProgressEvent("downloading", 100.0, "Leftover"))
        self.app.processEvents()

        # The download then reports its own progress, and the bar follows it.
        window._on_progress(ProgressEvent("downloading", 12.0, "Downloading media"))
        self.assertEqual(window.progress_bar.value(), 12)
        window.close()
        self.app.processEvents()

    def test_progress_from_the_current_worker_still_reaches_the_bar(self) -> None:
        # The guard above drops everything if it is written too eagerly, so the
        # passing case is asserted rather than assumed.
        window = MainWindow(FakeEngine())
        window._reset_progress_display()
        current = JobWorker(lambda emit, cancel: None)
        current.progress.connect(
            lambda event, owner=current: window.controller._relay_progress(owner, event)
        )
        window.controller._worker = current
        current._emit_progress(ProgressEvent("downloading", 33.0, "Downloading media"))
        self.app.processEvents()
        self.assertEqual(window.progress_bar.value(), 33)
        window.close()
        self.app.processEvents()

    def test_download_job_reenables_controls_after_thread_finishes(self) -> None:
        window = MainWindow(FakeEngine())
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self.assertTrue(window.controller.is_running)
            self._wait_for_job(window)
            self.assertFalse(window.controller.is_running)
            self.assertTrue(window.download_button.isEnabled())
            self.assertTrue((Path(directory) / "downloaded.mp4").is_file())
        window.close()

    def test_the_real_handoff_between_two_operations_moves_the_bar(self) -> None:
        # The recorded reason the earlier progress tests were not believed: they
        # call `_on_progress` directly, so they exercise the clamp but never the
        # transition.  This runs the two operations the way a person does - Fetch
        # details, then Download - through the real window and the real
        # controller, and watches the bar's `valueChanged` from a second thread
        # while the second job runs.
        #
        # Without the reset reaching the bar, the probe's 100% is still in
        # `_progress_value`, so the monotonic clamp in `_on_progress` pins the bar
        # there and the download appears frozen.
        engine = ProgressReportingEngine(FakeEngine())
        window = MainWindow(engine)
        window.url_edit.setText("https://youtu.be/id")
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)

            seen: list[int] = []
            window.progress_bar.valueChanged.connect(seen.append)

            window._fetch_details()
            self._wait_for_job(window)
            self.assertEqual(window.progress_bar.value(), 100, "the probe finished at 100%")

            # Recorded from here on, so nothing the probe did can be mistaken for
            # the download's own behaviour.
            seen.clear()
            window._start_download()
            self.assertTrue(window.controller.is_running)
            self._wait_for_job(window)

            self.assertTrue(engine.downloaded, "the download did not run")
            moved = sorted({value for value in seen if 0 < value < 100})
            self.assertTrue(
                moved,
                f"the bar never moved during the download; it showed {seen}. "
                "A probe that ended at 100% has pinned it.",
            )
            self.assertLess(max(moved), 100)
            self.assertEqual(window.progress_bar.value(), 100)
        window.close()

    def test_subtitle_mode_offers_the_languages_the_video_advertises(self) -> None:
        window = MainWindow(FakeEngine())
        subtitles_index = window.mode_combo.findData(DownloadMode.SUBTITLES.value)
        self.assertGreaterEqual(subtitles_index, 0)
        window.mode_combo.setCurrentIndex(subtitles_index)
        window.show()
        self.app.processEvents()
        # The caption controls only make sense for a captions mode.
        self.assertTrue(window.subtitle_format_combo.isVisibleTo(window))
        self.assertTrue(window.subtitle_source_combo.isVisibleTo(window))
        self.assertTrue(window.subtitle_language_combo.isVisibleTo(window))
        self.assertFalse(window.quality_combo.isVisibleTo(window))

        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        # The default source prefers author-written tracks, so only English.
        languages = [window.subtitle_language_combo.itemText(i) for i in range(window.subtitle_language_combo.count())]
        self.assertEqual(languages, ["English"])
        self.assertIn("one .srt file", window.summary_label.text())
        self.assertTrue(window.download_button.isEnabled())

        # Asking for the automatic tracks lists the auto-generated languages.
        window.subtitle_source_combo.setCurrentIndex(
            window.subtitle_source_combo.findData(SubtitleSource.AUTOMATIC.value)
        )
        self.app.processEvents()
        self.assertIn("(auto)", window.subtitle_language_combo.itemText(0))

        # "All" collapses to a single choice and reports a file per track.
        window.subtitle_source_combo.setCurrentIndex(
            window.subtitle_source_combo.findData(SubtitleSource.ALL.value)
        )
        self.app.processEvents()
        self.assertEqual(window.subtitle_language_combo.count(), 1)
        self.assertIn("2 .srt files", window.summary_label.text())

        # The format is part of the request and the reported file count.
        window.subtitle_format_combo.setCurrentIndex(
            window.subtitle_format_combo.findData(SubtitleFormat.VTT.value)
        )
        self.app.processEvents()
        self.assertIn(".vtt files", window.summary_label.text())

        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self._wait_for_job(window)
            self.assertTrue((Path(directory) / "downloaded.en.srt").is_file())
        window.close()
        self.app.processEvents()

    def test_stream_preference_recosts_every_quality_in_the_list(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(DownloadMode.VIDEO.value))
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)

        # The quality list is the only place a video size is shown now, so it
        # has to follow the preference instead of going on quoting the
        # best-quality number while the estimate underneath changes.
        best = window.quality_combo.itemText(0)
        self.assertIn(format_size(12_000_000), best)

        index = window.stream_preference_combo.findData(StreamPreference.SMALLER_FILE.value)
        self.assertGreaterEqual(index, 0)
        window.stream_preference_combo.setCurrentIndex(index)
        self.app.processEvents()
        self.assertEqual(window._selected_preference(), StreamPreference.SMALLER_FILE)
        self.assertIn(format_size(7_000_000), window.quality_combo.itemText(0))
        self.assertNotIn(format_size(12_000_000), window.quality_combo.itemText(0))
        # Switching the preference must not lose the chosen resolution.
        self.assertEqual(window.quality_combo.currentData().height, 720)
        window.close()
        self.app.processEvents()

    def test_smaller_file_is_withheld_where_youtube_offers_one_mp4_stream(self) -> None:
        # A video whose only 720p stream is a bloated HLS variant: there is no
        # leaner MP4 to switch to, so the choice must be disabled rather than
        # quietly handing back the same file.
        class SingleStreamEngine(FakeEngine):
            def probe(self, url: str, *, progress=None, cancel_check=None):
                return dataclasses.replace(
                    super().probe(url),
                    qualities=(
                        VideoQuality("v720", 720, 30, True, 1280, "mp4", 12_000_000),
                    ),
                )

        window = MainWindow(SingleStreamEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(DownloadMode.VIDEO.value))
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)

        index = window.stream_preference_combo.findData(StreamPreference.SMALLER_FILE.value)
        self.assertGreaterEqual(index, 0)
        self.assertFalse(window.stream_preference_combo.model().item(index).isEnabled())
        # "only one H.264 stream", not the "only one MP4 stream" this asserted
        # before.  The constraint is the codec, not the container: a smaller MP4
        # stream in VP9 or AV1 is refused for its codec alone, so saying "MP4"
        # implied no smaller MP4 existed when one often does.
        self.assertIn("only one H.264 stream", window.stream_preference_combo.toolTip())
        window.stream_preference_combo.setCurrentIndex(index)
        self.app.processEvents()
        # A choice that cannot apply must not survive into the request.
        self.assertIs(window._selected_preference(), StreamPreference.QUALITY)
        window.close()
        self.app.processEvents()

    def test_a_queue_keeps_smaller_file_offered_before_anything_is_probed(self) -> None:
        # Each queued link resolves its own preference when it is reached, so
        # no untested link can be ruled out in advance.
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(DownloadMode.QUEUE.value))
        window._mode_changed()
        self.app.processEvents()
        index = window.stream_preference_combo.findData(StreamPreference.SMALLER_FILE.value)
        self.assertTrue(window.stream_preference_combo.model().item(index).isEnabled())
        window.close()
        self.app.processEvents()

    def test_queue_mode_deduplicates_links_and_reports_each_outcome(self) -> None:
        window = MainWindow(FakeEngine())
        queue_index = window.mode_combo.findData(DownloadMode.QUEUE.value)
        self.assertGreaterEqual(queue_index, 0)
        window.mode_combo.setCurrentIndex(queue_index)
        window.show()
        self.app.processEvents()
        self.assertTrue(window.queue_group.isVisibleTo(window))
        self.assertFalse(window.playlist_group.isVisibleTo(window))
        # A batch is not probed up front, so the single-video link box is off.
        self.assertFalse(window.url_edit.isEnabled())
        # With no links there is nothing to download.
        self.assertFalse(window.download_button.isEnabled())

        window.queue_edit.setPlainText(
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "\n"
            "https://youtu.be/bbbbbbbbbbb\n"
            "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "not a link\n"
        )
        self.app.processEvents()
        self.assertEqual(
            window._queue_links(),
            (
                "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                "https://youtu.be/bbbbbbbbbbb",
            ),
        )
        self.assertEqual(window.queue_table.rowCount(), 2)
        self.assertIn("2 links queued", window.queue_summary_label.text())
        self.assertIn("1 duplicate ignored", window.queue_summary_label.text())
        self.assertIn("1 line not a YouTube link", window.queue_summary_label.text())
        self.assertTrue(window.download_button.isEnabled())
        # Heights are fixed for a batch, so each link resolves to the nearest.
        self.assertEqual(window.quality_combo.count(), 6)
        self.assertEqual(window.quality_combo.currentData(), PlaylistQuality(height=1080, fps=None))

        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self._wait_for_job(window)
            self.assertIn("2 of 2 links saved", window.status_label.text())
            self.assertEqual(window.queue_table.item(0, QUEUE_COLUMN_STATUS).text(), "Saved")
            self.assertEqual(window.queue_table.item(0, QUEUE_COLUMN_TITLE).text(), "Video 1")
            self.assertIn("B", window.queue_table.item(0, QUEUE_COLUMN_SIZE).text())
            self.assertTrue((Path(directory) / "queued-1.mp4").is_file())
        window.close()
        self.app.processEvents()

    def test_queue_marks_a_failed_link_and_keeps_going(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(DownloadMode.QUEUE.value))
        window.queue_edit.setPlainText(
            "https://youtu.be/aaaaaaaaaaa\n"
            "https://youtu.be/badbbbbbbbbb\n"
            "https://youtu.be/ccccccccccc\n"
        )
        self.app.processEvents()
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self._wait_for_job(window)
            self.assertIn("2 of 3 links saved", window.status_label.text())
            self.assertIn("1 failed and skipped", window.status_label.text())
            self.assertEqual(window.queue_table.item(1, QUEUE_COLUMN_STATUS).text(), "Failed")
            self.assertIn("could not be read", window.queue_table.item(1, QUEUE_COLUMN_SIZE).text())
            # The link after the failure was still saved.
            self.assertEqual(window.queue_table.item(2, QUEUE_COLUMN_STATUS).text(), "Saved")
            self.assertTrue((Path(directory) / "queued-3.mp4").is_file())
        window.close()
        self.app.processEvents()

    def test_open_folder_and_play_buttons_follow_the_last_download(self) -> None:
        window = MainWindow(FakeEngine())
        self.assertFalse(window.open_folder_button.isEnabled())
        self.assertFalse(window.play_button.isEnabled())
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self._wait_for_job(window)
            self.assertTrue(window.open_folder_button.isEnabled())
            self.assertTrue(window.play_button.isEnabled())
            self.assertIn("downloaded.mp4", window.play_button.toolTip())

            # A captions-only result is not something a player can open, so
            # Play turns off while Open folder stays available.
            caption = Path(directory) / "downloaded.en.srt"
            caption.write_text("1\n", encoding="utf-8")
            window._on_job_succeeded(DownloadResult(caption, DownloadMode.SUBTITLES, (caption,)))
            self.assertTrue(window.open_folder_button.isEnabled())
            self.assertFalse(window.play_button.isEnabled())
        window.close()
        self.app.processEvents()

    def test_playlist_can_save_caption_files_for_every_selected_video(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(DownloadMode.PLAYLIST.value))
        window.url_edit.setText("https://www.youtube.com/playlist?list=PL123")
        window._fetch_details()
        self._wait_for_job(window)
        media_index = window.playlist_media_combo.findData(PlaylistMedia.SUBTITLES.value)
        self.assertGreaterEqual(media_index, 0)
        window.playlist_media_combo.setCurrentIndex(media_index)
        self.app.processEvents()
        self.assertTrue(window.subtitle_format_combo.isVisibleTo(window))
        self.assertFalse(window.quality_combo.isVisibleTo(window))
        self.assertTrue(window.download_button.isEnabled())
        # The unavailable playlist entry stays unselectable in caption mode too.
        removed_row = 2
        check = window.playlist_table.item(removed_row, PLAYLIST_COLUMN_CHECK)
        self.assertEqual(check.checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(check.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        window.close()
        self.app.processEvents()

    def test_every_mode_has_a_playlist_that_never_appears_in_a_batch(self) -> None:
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        mode_values = {
            window.mode_combo.itemData(index) for index in range(window.mode_combo.count())
        }
        self.assertEqual(
            mode_values,
            {mode.value for mode in DownloadMode},
        )
        # A batch is a list of single links, so it must not offer a playlist
        # or another batch as a per-link media kind.
        queue_media = {
            window.queue_media_combo.itemData(index)
            for index in range(window.queue_media_combo.count())
        }
        self.assertNotIn(DownloadMode.PLAYLIST.value, queue_media)
        self.assertNotIn(DownloadMode.QUEUE.value, queue_media)
        self.assertIn(DownloadMode.SUBTITLES.value, queue_media)
        window.close()
        self.app.processEvents()

    def test_theme_selector_lists_every_theme_and_applies_it(self) -> None:
        window = MainWindow(FakeEngine())
        expected = [theme_id for theme_id, _label, _description in THEMES]
        self.assertEqual(
            [window.theme_combo.itemData(index) for index in range(window.theme_combo.count())],
            expected,
        )
        # The default must be the first entry so the selector starts coherent.
        self.assertEqual(window.theme_combo.currentData(), expected[0])
        for index, theme_id in enumerate(expected):
            with self.subTest(theme=theme_id):
                window.theme_combo.setCurrentIndex(index)
                self.app.processEvents()
                self.assertEqual(window._theme, theme_id)
                self.assertIn("QProgressBar", window.styleSheet())
                # A row must never be blank in the dropdown.
                self.assertTrue(window.theme_combo.itemText(index).strip())
        window.close()
        self.app.processEvents()

    def test_theme_button_is_wide_enough_for_its_longest_name(self) -> None:
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        metrics = window.theme_combo.fontMetrics()
        for index in range(window.theme_combo.count()):
            text = window.theme_combo.itemText(index)
            with self.subTest(theme=text):
                # A name that does not fit gets elided by Qt, so the control has
                # to be at least as wide as the text it is asked to show.
                self.assertGreaterEqual(
                    window.theme_combo.width(),
                    metrics.horizontalAdvance(text),
                )
            # The width is derived from the list, not chosen by hand.
            self.assertEqual(
                window.theme_combo.width(),
                window._theme_combo_target(),
            )
            # The label beside it matches the control it points at.
            self.assertEqual(
                window.theme_label.height(),
                window.theme_combo.sizeHint().height(),
            )
        window.close()
        self.app.processEvents()

    def test_the_chosen_theme_is_remembered_for_the_next_launch(self) -> None:
        # The point of the setting: whatever the user picks is what they get
        # every time they open the program, with nothing to configure.  The
        # settings functions themselves are the real ones, pointed at a
        # temporary file by setUp, so this exercises the whole path.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        # Constructing a window must not decide anything on the user's behalf.
        self.assertFalse(self.settings_path.exists())
        window.theme_combo.setCurrentIndex(window.theme_combo.findData("gruvbox"))
        self.app.processEvents()
        self.assertEqual(window._theme, "gruvbox")
        window.close()
        self.app.processEvents()
        self.assertEqual(
            main_window.read_settings(), {THEME_SETTING_KEY: "gruvbox"}
        )

        # A second window, as if the program had been closed and reopened.
        restored = MainWindow(FakeEngine())
        restored.show()
        self.app.processEvents()
        self.assertEqual(restored._theme, "gruvbox")
        self.assertEqual(restored.theme_combo.currentData(), "gruvbox")
        restored.close()
        self.app.processEvents()

    def test_a_saved_theme_that_no_longer_exists_falls_back(self) -> None:
        # A real case, not a hypothetical one: the two see-through themes were
        # removed, so anyone who had picked one still has its id on disk.
        self.settings_path.write_text(
            json.dumps({THEME_SETTING_KEY: "glass"}), encoding="utf-8"
        )
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        self.assertEqual(window._theme, DEFAULT_THEME)
        self.assertEqual(window.theme_combo.currentData(), DEFAULT_THEME)
        window.close()
        self.app.processEvents()

    def test_no_theme_leaves_a_clear_pixel_in_the_window(self) -> None:
        # Checked against actually rendered pixels rather than against the
        # stylesheet text.  Reading the stylesheet is not enough: Qt's cascade is
        # specificity-based, and a descendant selector naming the card outranks
        # the card's own rule, so the card can be painted clear while every rule
        # in the sheet looks right.  That is exactly the bug this test was
        # written for, and it is why it renders rather than greps.
        for theme_id, _label, _description in THEMES:
            with self.subTest(theme=theme_id):
                window = MainWindow(FakeEngine())
                window.resize(900, 700)
                window._apply_theme(theme_id)
                window.show()
                self.app.processEvents()
                image = QImage(window.width(), window.height(), QImage.Format.Format_ARGB32)
                image.fill(0)
                window.render(image)
                # No margin: the card is the whole window, edge to edge.
                self.assertEqual(window._shell_layout.contentsMargins().left(), 0)
                mid = window.height() // 2
                alphas = [
                    image.pixelColor(x, mid).alpha()
                    for x in range(0, window.width(), 4)
                ]
                self.assertEqual(
                    min(alphas), 255, f"{theme_id} has a clear pixel in the window"
                )
                window.close()
                self.app.processEvents()

    def test_window_draws_its_own_frame_instead_of_the_native_one(self) -> None:
        # The window drops the native frame and draws its own caption.  The
        # replacement has to work: the buttons must be there and wired to real
        # actions.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        self.assertTrue(
            window.windowFlags() & Qt.WindowType.FramelessWindowHint
        )
        self.assertEqual(window.title_bar.objectName(), "titleBar")
        for button in (
            window.minimize_button,
            window.maximize_button,
            window.close_button,
        ):
            self.assertTrue(button.isVisibleTo(window))
            self.assertTrue(button.toolTip().strip())
            # No text: the glyph is painted, because a text glyph is laid out
            # against the font's baseline and the three marks end up at three
            # different heights, which is what made them read as typed labels.
            self.assertEqual(button.text(), "")
            self.assertIsInstance(button, CaptionButton)
            self.assertIn(
                button.glyph(),
                (caption.MINIMIZE, caption.MAXIMIZE, caption.RESTORE, caption.CLOSE),
            )
        # The caption is a drag handle, and the grip has to stay narrower than
        # the bar or dragging the window would resize it instead.
        self.assertLess(window._resize_border(), window.title_bar.height())
        window.close()
        self.app.processEvents()

    def test_every_caption_glyph_is_actually_painted(self) -> None:
        # A painted glyph has no text for a stylesheet to colour and nothing in
        # the source that proves it will be drawn, so it is rendered and the ink
        # counted.  The button's own background is transparent, so a widget that
        # paints nothing comes back empty however correct its code reads - which
        # is exactly the failure mode of moving from a font glyph to QPainter.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()

        def inked(button: CaptionButton) -> int:
            image = button.grab().toImage()
            # Any ink at all.  A high threshold would count only the pixels a
            # 1.4px pen happens to land dead centre on: the edges of the
            # maximize square and the whole of the minimize line fall between
            # pixel centres and come out half-covered, which is correct
            # antialiasing and not a missing glyph.
            return sum(
                1
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 8
            )

        try:
            marks = {name: inked(getattr(window, name)) for name in
                     ("minimize_button", "maximize_button", "close_button")}
            for name, count in marks.items():
                self.assertGreater(count, 20, f"{name} painted nothing: {marks}")
            # Two squares must be more ink than one, which is the only way to tell
            # that the restore mark really is two marks and not the maximize one
            # left behind when the window state changed.
            window.showMaximized()
            self.app.processEvents()
            self.assertEqual(window.maximize_button.glyph(), caption.RESTORE)
            self.assertGreater(inked(window.maximize_button), marks["maximize_button"])
            window.showNormal()
            self.app.processEvents()
            self.assertEqual(window.maximize_button.glyph(), caption.MAXIMIZE)
        finally:
            window.close()
            self.app.processEvents()

    def test_the_caption_sits_outside_the_scrolling_area(self) -> None:
        # The caption used to be the first row of the scrolling card, which put
        # the scroll bar the full height of the window right beside the window
        # buttons and made the caption itself scroll out of sight.  A caption is
        # fixed furniture: it has to be a sibling of the scroll area, above it.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            scroll_area = window._scroll_area
            card = scroll_area.widget()
            self.assertIsNotNone(card)
            # Not inside the card, and not a descendant of it at any depth.
            self.assertIsNot(window.title_bar, card)
            self.assertNotEqual(window.title_bar.parentWidget(), card)
            self.assertFalse(window.title_bar.isAncestorOf(scroll_area))
            self.assertEqual(
                scroll_area.parentWidget(),
                window.title_bar.parentWidget(),
                "the caption and the scroll area have to share a parent to be stacked",
            )
            # Above it, not overlapping it, and the scroll area takes the rest.
            bar_geometry = window.title_bar.geometry()
            scroll_geometry = scroll_area.geometry()
            self.assertEqual(bar_geometry.top(), 0)
            self.assertEqual(bar_geometry.height(), TITLE_BAR_HEIGHT)
            self.assertEqual(scroll_geometry.top(), TITLE_BAR_HEIGHT)
            self.assertLessEqual(scroll_geometry.bottom(), window.height())
            # And the bar stays put when the content is scrolled.
            bar_before = window.title_bar.geometry()
            scroll_area.verticalScrollBar().setValue(
                scroll_area.verticalScrollBar().maximum()
            )
            self.app.processEvents()
            self.assertEqual(window.title_bar.geometry(), bar_before)
        finally:
            window.close()
            self.app.processEvents()

    def test_the_scroll_bar_stays_inside_the_content(self) -> None:
        # The bar belongs to the content it scrolls.  When the caption sat inside
        # the scroll area the bar ran from the very top of the window to the very
        # bottom, alongside the window buttons, which is both wrong-looking and
        # wrong as a control: a bar that tall suggests far more content than there
        # is, and its top end sits where a user expects the title to be.
        window = MainWindow(FakeEngine())
        window.resize(760, 540)
        window.show()
        self.app.processEvents()
        try:
            content = window._scroll_area
            bar = content.verticalScrollBar()
            bar_geometry = bar.geometry()
            # In the window's coordinates the bar starts below the caption
            # strip rather than at the very top of the window, which is where it
            # used to start, level with the window buttons.
            top_in_window = bar.mapTo(window, QPoint(0, 0)).y()
            self.assertGreaterEqual(top_in_window, TITLE_BAR_HEIGHT)
            # It still reaches the bottom of the content it scrolls.
            bottom_in_window = bar.mapTo(window, QPoint(0, 0)).y() + bar_geometry.height()
            self.assertEqual(bottom_in_window, window.height())
            # Only as tall as the content, not as tall as the window.
            self.assertLess(bar_geometry.height(), window.height())
        finally:
            window.close()
            self.app.processEvents()

    def test_the_window_buttons_sit_flush_in_the_top_right_corner(self) -> None:
        # Windows puts its three caption buttons against the top-right corner
        # with no gutter and no rounding, full height, and the close button at the
        # very edge.  Anywhere else and they read as application buttons that
        # happen to be near the top.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            buttons = [
                window.minimize_button,
                window.maximize_button,
                window.close_button,
            ]
            widths = {button.width() for button in buttons}
            heights = {button.height() for button in buttons}
            self.assertEqual(len(widths), 1, "the three buttons must be one width")
            self.assertEqual(len(heights), 1, "the three buttons must be one height")
            self.assertEqual(
                heights, {TITLE_BAR_HEIGHT}, "a caption highlight covers the whole bar"
            )
            for button in buttons:
                geometry = button.geometry()
                self.assertEqual(geometry.top(), 0)
                self.assertEqual(geometry.height(), TITLE_BAR_HEIGHT)
            # Flush against the right edge, and no gap between neighbours.  The
            # title bar is the central widget's first row with no margin, so a
            # button's x inside it is its x inside the window.
            width = buttons[0].width()
            self.assertEqual(
                [button.x() for button in buttons],
                [
                    window.width() - 3 * width,
                    window.width() - 2 * width,
                    window.width() - width,
                ],
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_window_buttons_have_no_box_of_their_own(self) -> None:
        # Every cue that says "an application button" - a border, a resting fill,
        # rounded corners - is what made these three read as typed labels with a
        # frame.  At rest they are a glyph on the title bar and nothing else.
        for theme_id, _label, _description in THEMES:
            with self.subTest(theme=theme_id):
                window = MainWindow(FakeEngine())
                window._apply_theme(theme_id)
                window.show()
                self.app.processEvents()
                try:
                    sheet = window.styleSheet()
                    for name in (
                        "QPushButton#windowButton",
                        "QPushButton#windowButtonMaximize",
                        "QPushButton#windowButtonClose",
                    ):
                        rule = self._rule_for(sheet, name)
                        self.assertIn("background: transparent", rule)
                        self.assertIn("border: none", rule)
                        self.assertIn("border-radius: 0px", rule)
                    # The close button keeps the shell's own hover colour, which
                    # is the one thing that makes it the close button.
                    self.assertIn("#c42b1c", sheet)
                finally:
                    window.close()
                    self.app.processEvents()

    @staticmethod
    def _rule_for(sheet: str, selector: str) -> str:
        """The declaration block that belongs to one selector.

        Selectors may be grouped, one per line, each but the last ending in a
        comma, which is how the three window buttons share a base rule while the
        close button still gets a hover rule of its own.  A grouped line is
        therefore only part of a match when the whole line is the selector, and
        never when it carries a pseudo-state: ``QScrollBar::handle:vertical`` is
        not styled by the ``:hover`` rule that follows it, and treating it as
        though it were would check the hover geometry against the resting one.
        """

        text = sheet.replace("{{", "{").replace("}}", "}")
        offset = 0
        while True:
            position = text.find(selector, offset)
            if position < 0:
                raise AssertionError(f"{selector} is not in the style sheet at all")
            line_end = text.find("\n", position)
            line = text[position : line_end if line_end >= 0 else len(text)]
            # Drop a trailing comma or brace, and the space the brace is preceded
            # by, so the line is compared as the bare selector.
            token = line.strip().rstrip(",").strip().rstrip("{").strip()
            if token == selector:
                brace = text.index("{", position)
                return text[brace + 1 : text.index("}", brace)]
            offset = position + len(selector)

    def test_help_offers_a_way_back_to_the_ffmpeg_offer(self) -> None:
        # Declining the first-run prompt used to be close to a dead end: the check
        # ran before the window existed and nothing else re-ran it, so the only
        # recovery was to close and reopen the program.
        window = MainWindow(FakeEngine())
        try:
            labels = [action.text() for action in window.help_button.menu().actions()]
            self.assertIn("&Check dependencies again", labels)
            self.assertIn("&Remove the FFmpeg ClipDock installed", labels)
            self.assertIn("Check for ClipDock &updates", labels)
        finally:
            window.close()
            self.app.processEvents()

    def test_a_finished_update_check_is_not_reported_as_a_finished_download(self) -> None:
        # Every job result lands in one handler, and its last branch assumes a
        # DownloadResult.  An UpdateCheck reaching it would set the bar to 100%
        # and say "Download complete" with no file behind it.
        window = MainWindow(FakeEngine())
        try:
            reported: list[UpdateCheck] = []
            # The handler says whether the check was automatic, so the stub
            # takes the option rather than dropping it silently.
            window._report_update = lambda result, **options: reported.append(result)
            window._on_job_succeeded(
                UpdateCheck(status="current", current="0.1.0", latest="v0.1.0")
            )
            self.assertEqual([result.status for result in reported], ["current"])
            self.assertNotIn("Download complete", window.status_label.text())
        finally:
            window.close()
            self.app.processEvents()

    def test_the_update_check_is_refused_while_something_else_is_running(self) -> None:
        # A download owns the engine and the progress bar; interleaving a check
        # with it would fight over both for no reason.
        window = MainWindow(FakeEngine())
        try:
            started: list[object] = []
            window.controller.start = lambda operation: started.append(operation)
            # is_running is derived from the thread, so it is a property and has
            # to be replaced on the class rather than assigned on the instance.
            with patch.object(
                type(window.controller), "is_running", new_callable=PropertyMock, return_value=True
            ), patch("youtube_downloader.gui.main_window.QMessageBox.information") as told:
                window._check_for_updates()
            self.assertEqual(started, [], "a check was started during a running job")
            told.assert_called_once()
        finally:
            window.close()
            self.app.processEvents()

    # -- the automatic update check -----------------------------------------

    @staticmethod
    def _record_starts(window: MainWindow) -> list[Any]:
        """Swap the controller's `start` for a recorder.

        The real one runs the operation on a thread, and the operation for an
        update check is a live request to api.github.com.  Recording instead of
        running means everything the window does *around* the request is
        exercised while no test in the suite is capable of making one.
        """

        started: list[Any] = []
        window.controller.start = started.append  # type: ignore[method-assign]
        return started

    def test_the_automatic_check_waits_for_the_window_and_spends_nothing_arming(self) -> None:
        # "After the window is up and idle rather than during startup": a
        # delay is the whole of that requirement, and a delay of zero would be
        # startup.  Arming it writes nothing, because a launch that has not
        # asked a question has not spent the day's question.
        window = MainWindow(FakeEngine())
        try:
            self.assertFalse(
                window._update_check_timer.isActive(),
                "building a window scheduled a network request",
            )
            self.assertTrue(window.schedule_update_check())
            timer = window._update_check_timer
            self.assertTrue(timer.isActive())
            self.assertTrue(timer.isSingleShot())
            self.assertGreater(
                timer.interval(), 0, "a check at zero delay happens during startup"
            )
            self.assertFalse(
                self.settings_path.exists(),
                "arming the check recorded it as having run",
            )
        finally:
            window.close()
            self.app.processEvents()
        self.assertFalse(
            window._update_check_timer.isActive(),
            "closing disarms a check nobody would ever be told about",
        )

    def test_the_automatic_check_is_a_switch_sits_beside_the_manual_check(self) -> None:
        window = MainWindow(FakeEngine())
        try:
            labels = [action.text() for action in window.help_button.menu().actions()]
            self.assertIn("Check for updates &automatically", labels)
            self.assertIn("Check for ClipDock &updates", labels)
            # On by default, because that is what was decided - and the action
            # starts life agreeing with the file rather than asserting itself
            # over it.
            self.assertTrue(window._auto_update_action.isChecked())

            window._auto_update_action.setChecked(False)
            self.assertEqual(
                main_window.read_settings().get(main_window.UPDATE_CHECK_SETTING_KEY),
                "0",
                "the switch was shown as off without being remembered as off",
            )
            self.assertFalse(
                window.schedule_update_check(),
                "a check the person refused to allow was armed anyway",
            )
            self.assertFalse(window._update_check_timer.isActive())

            window._auto_update_action.setChecked(True)
            self.assertEqual(
                main_window.read_settings().get(main_window.UPDATE_CHECK_SETTING_KEY), "1"
            )
            self.assertTrue(window.schedule_update_check())
            self.assertTrue(window._update_check_timer.isActive())
        finally:
            window.close()
            self.app.processEvents()

    def test_the_automatic_check_asks_at_most_once_a_day(self) -> None:
        # The date is the promise that this cannot become a nag: one request a
        # day at most, recorded when the attempt starts so that a machine with
        # no network is not asked again on every single launch.
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            started = self._record_starts(window)
            self.assertTrue(window.schedule_update_check())
            window._run_scheduled_update_check()
            self.assertEqual(len(started), 1, "the first attempt did not ask")
            self.assertEqual(
                main_window.read_settings().get(main_window.UPDATE_CHECK_DATE_KEY),
                date.today().isoformat(),
            )

            window._update_check_timer.stop()
            self.assertFalse(window.schedule_update_check())
            window._run_scheduled_update_check()
            self.assertEqual(
                len(started),
                1,
                "it asked a second time on the day it had already asked",
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_automatic_check_refuses_to_race_a_download(self) -> None:
        # Eight seconds is time enough for the person to have started
        # something.  A refused check also must not record itself as having
        # run: somebody else's download decided this launch's answer, so the
        # next launch still gets to ask.
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            started = self._record_starts(window)
            with patch.object(
                type(window.controller),
                "is_running",
                new_callable=PropertyMock,
                return_value=True,
            ):
                self.assertFalse(window.schedule_update_check())
                window._run_scheduled_update_check()
            self.assertEqual(started, [])
            self.assertFalse(
                self.settings_path.exists(),
                "a check that was refused recorded itself as done",
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_an_automatic_check_says_nothing_when_there_is_nothing_to_say(self) -> None:
        # Three ways for the answer to be "no news": the release already
        # installed is the latest, GitHub could not be reached, and GitHub
        # refused.  None of them is worth interrupting anybody for, and the
        # third is the one a dialog would have been most tempted by - which
        # would be a connectivity problem presented as an application problem.
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            status_before = window.status_bar.currentMessage()
            label_before = window.status_label.text()
            with patch("youtube_downloader.gui.main_window.QMessageBox") as boxes:
                for result in (
                    UpdateCheck(status="current", current="0.2.0", latest="v0.2.0"),
                    UpdateCheck(
                        status="unknown",
                        current="0.2.0",
                        latest=None,
                        reason="this computer could not reach GitHub.",
                    ),
                    UpdateCheck(
                        status="unknown",
                        current="0.2.0",
                        latest=None,
                        reason="GitHub refused the request. This is usually its rate limit.",
                    ),
                ):
                    window._report_update(result, automatic=True)
                boxes.assert_not_called()
            self.assertIsNone(window._update_toast, "nothing was worth a notice")
            self.assertEqual(window.status_bar.currentMessage(), status_before)
            self.assertEqual(window.status_label.text(), label_before)
        finally:
            window.close()
            self.app.processEvents()

    def test_an_available_release_is_offered_once_in_the_corner_and_never_installed(
        self,
    ) -> None:
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            status_before = window.status_bar.currentMessage()
            window._report_update(
                UpdateCheck(
                    status="newer",
                    current="0.2.0",
                    latest="0.3.0",
                    release_url="https://example.invalid/clipdock/releases/tag/0.3.0",
                ),
                automatic=True,
            )
            toast = window._update_toast
            self.assertIsNotNone(toast, "the one thing the automatic check may say went unsaid")
            assert toast is not None
            self.assertTrue(toast.isVisibleTo(window))
            self.assertEqual(window.status_bar.currentMessage(), status_before)

            heading = window._update_toast_heading.text()
            detail = window._update_toast_detail.text()
            self.assertIn("0.3.0", heading)
            self.assertIn("You have 0.2.0", detail)
            # The banner must not imply urgency it has not got.  A release
            # existing is a fact about a web page; it is not an event, and the
            # program has no grounds to call it one.
            for overstatement in (
                "now!",
                "urgent",
                "immediately",
                "important",
                "act now",
                "don't miss",
                "update now",
            ):
                self.assertNotIn(overstatement, f"{heading} {detail}".lower())

            buttons = toast.findChildren(QPushButton)
            self.assertEqual(
                [button.objectName() for button in buttons],
                ["updateToastClose", "updateToastOpen"],
                "the notice offers something other than opening a web page",
            )
            texts = [button.text() for button in buttons]
            self.assertIn("Open the release page", texts)
            for button in buttons:
                self.assertNotIn("install", button.text().lower())
                self.assertNotIn("update now", button.text().lower())
            note = toast.findChild(QLabel, "updateToastNote")
            self.assertIsNotNone(note, "the notice never says what it did not do")
            assert note is not None
            self.assertIn("has not downloaded anything", note.text())

            # It sits in the corner it was promised: inside the window, and
            # clear of the status line rather than painted over it.
            geometry = toast.geometry()
            status_top = window.status_bar.mapTo(window, QPoint(0, 0)).y()
            self.assertTrue(window.rect().contains(geometry), "the notice is off screen")
            self.assertLessEqual(geometry.bottom(), status_top, "the notice covers the status line")

            # Dismissing it hides it, which is the whole of "dismissible".
            next(button for button in buttons if button.objectName() == "updateToastClose").click()
            self.assertFalse(toast.isVisibleTo(window))

            # And the only thing it can open is a page, over https.
            with patch(
                "youtube_downloader.gui.main_window.QDesktopServices.openUrl"
            ) as opened:
                window._open_update_toast_page()
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args[0][0].scheme(), "https")
            self.assertEqual(
                opened.call_args[0][0].toString(),
                "https://example.invalid/clipdock/releases/tag/0.3.0",
            )

            window._update_toast_url = "http://example.invalid/not-https"
            with patch(
                "youtube_downloader.gui.main_window.QDesktopServices.openUrl"
            ) as opened:
                window._open_update_toast_page()
            self.assertEqual(
                opened.call_args[0][0].toString(),
                RELEASES_URL,
                "a link that is not https was handed to the desktop",
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_automatic_check_leaves_the_progress_bar_and_the_status_alone(self) -> None:
        # The manual check puts the bar into its indeterminate state and has to
        # put it back; the automatic one never touched it, and filling it would
        # be the program reporting progress on a job that was invisible by
        # design.  Both halves are asserted here, because a difference only
        # one of them has is a difference nobody would notice until it was
        # wrong.
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            status_before = window.status_bar.currentMessage()
            self._record_starts(window)
            bar_before = (
                window.progress_bar.minimum(),
                window.progress_bar.maximum(),
                window.progress_bar.value(),
            )

            window._update_check_automatic = True
            window._set_busy(True)
            window._on_job_succeeded(
                UpdateCheck(status="current", current="0.2.0", latest="v0.2.0")
            )
            self.assertFalse(
                window._update_check_automatic,
                "the flag outlived the result it was set for",
            )
            self.assertEqual(window.status_bar.currentMessage(), status_before)
            self.assertEqual(
                (
                    window.progress_bar.minimum(),
                    window.progress_bar.maximum(),
                    window.progress_bar.value(),
                ),
                bar_before,
                "the automatic check moved a bar for a job nobody saw",
            )
            self.assertTrue(window.fetch_button.isEnabled())

            # The manual path still restores what it changed.  Its dialog is
            # replaced rather than opened: a modal box with nobody to click it
            # blocks until it is closed, which is exactly what the real check
            # is meant to wait for and what this test must not.
            window._set_busy(True)
            window._reset_progress_display()
            self.assertEqual(
                (window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0)
            )
            with patch("youtube_downloader.gui.main_window.QMessageBox"):
                window._on_job_succeeded(
                    UpdateCheck(status="current", current="0.2.0", latest="v0.2.0")
                )
            self.assertEqual(
                (window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 100)
            )
            self.assertEqual(window.progress_bar.value(), 100)
        finally:
            window.close()
            self.app.processEvents()

    def test_a_failed_automatic_check_reports_nothing_at_all(self) -> None:
        # `check_for_updates` documents that it never raises, so this should be
        # unreachable.  It is asserted anyway because the promise is that an
        # automatic check cannot interrupt anybody, and an unattended modal
        # dialog is exactly that.
        window = MainWindow(FakeEngine())
        try:
            window.show()
            self.app.processEvents()
            status_before = window.status_bar.currentMessage()
            window._update_check_automatic = True
            window._set_busy(True)
            with patch("youtube_downloader.gui.main_window.QMessageBox") as boxes:
                window._on_job_failed(RuntimeError("nope"))
            boxes.assert_not_called()
            self.assertEqual(window.status_bar.currentMessage(), status_before)
            self.assertFalse(window._update_check_automatic)
            self.assertTrue(
                window.fetch_button.isEnabled(),
                "the refused check never gave the window back",
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_automatic_check_is_actually_scheduled_by_the_launcher(self) -> None:
        # Nothing else reaches it.  Every other test builds a window by hand
        # and would never arm it, so a check dropped from the launch path would
        # leave a complete, passing test suite around a feature that no longer
        # happens - which is how "runs on startup" quietly becomes "ran once".
        source = (Path(main_window.__file__).resolve().parents[1] / "cli.py").read_text(
            encoding="utf-8"
        )
        shown = source.find("window.show()")
        scheduled = source.find("window.schedule_update_check()")
        loop = source.find("return app.exec()")
        self.assertNotEqual(shown, -1, "the launcher no longer shows the window")
        self.assertNotEqual(scheduled, -1, "the launcher never arms the update check")
        self.assertNotEqual(loop, -1, "the launcher no longer enters the event loop")
        self.assertLess(shown, scheduled, "the check is armed before the window is up")
        self.assertLess(
            scheduled, loop, "the event loop starts before the check can be armed"
        )

    def test_the_help_menu_sits_in_the_caption_strip(self) -> None:
        # A native menu bar above a drawn caption would read as a second frame.
        window = MainWindow(FakeEngine())
        try:
            self.assertEqual(window.help_button.objectName(), "helpButton")
            # menuWidget() reports whether one was installed; menuBar() would
            # *create* one to answer the question, which is the opposite of
            # checking that the window did not ask for one.
            self.assertIsNone(window.menuWidget())
            self.assertEqual(window.help_button.height(), TITLE_BAR_HEIGHT)
            self.assertTrue(
                window.help_button.geometry().intersects(window.title_bar.geometry()),
                "the Help trigger is not in the title bar it belongs to",
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_help_button_is_styled_like_a_caption_button(self) -> None:
        window = MainWindow(FakeEngine())
        try:
            rule = self._rule_for(window.styleSheet(), "QPushButton#helpButton")
            self.assertIn("background: transparent", rule)
            self.assertIn("border: none", rule)
            self.assertIn("border-radius: 0px", rule)
            # The arrow is hidden because the button opens a menu on press, so an
            # indicator would describe a control the user does not have to aim at.
            self.assertIn("QPushButton#helpButton:menu-indicator", window.styleSheet())
            # No colour and no font-size, because there is no text for either to
            # apply to.  A stylesheet that sets them is describing a glyph the
            # button no longer has, which is how the painted "?" quietly stopped
            # taking the theme's colour and became unreadable on one theme.
            self.assertNotIn("font-size", rule)
            self.assertNotIn("color", rule)
        finally:
            window.close()
            self.app.processEvents()

    def test_the_help_mark_is_painted_rather_than_typed(self) -> None:
        # It was a literal "?" in the button's font.  `caption.py` argues against
        # exactly that - a text glyph is laid out and baseline-aligned by the
        # font, so it lands at a different weight from the three marks beside it -
        # and a character the font lacks draws as a box, which this program's own
        # screenshot tool has already been bitten by once.
        window = MainWindow(FakeEngine())
        try:
            self.assertIsInstance(window.help_button, CaptionButton)
            self.assertEqual(window.help_button.glyph(), caption.OVERFLOW)
            self.assertEqual(window.help_button.text(), "")
        finally:
            window.close()
            self.app.processEvents()

    def test_the_help_mark_is_recoloured_by_every_theme(self) -> None:
        # The mark is painted, so no stylesheet can colour it; `set_caption_colors`
        # is the only thing that can.  Leaving the button out of that list is what
        # would leave it in whatever colour the theme started with.
        #
        # This checks the colour the button was *told* to use, which is not the
        # same question as the one that actually mattered - see
        # test_the_help_mark_is_painted_in_the_theme_colour.
        window = MainWindow(FakeEngine())
        window.show()
        try:
            for theme_id, _label, _description in THEMES:
                with self.subTest(theme=theme_id):
                    window._apply_theme(theme_id)
                    self.app.processEvents()
                    expected = theme_palette(theme_id)["text"].lower()
                    for name in ("help_button", "minimize_button", "close_button"):
                        with self.subTest(button=name):
                            button = getattr(window, name)
                            self.assertEqual(
                                button._glyph_color.name().lower(),
                                expected,
                                f"the {name} mark is not the theme's text colour",
                            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_help_mark_is_painted_in_the_theme_colour(self) -> None:
        # The dot is drawn with the brush rather than a pen, so nothing about
        # `_glyph_color` proves anything about what reaches the screen.  When the
        # brush was filled from `painter.pen().color()` *after* `setPen(NoPen)`,
        # every theme painted black - `setPen` does not switch the pen off, it
        # installs a new pen carrying a default black - while `_glyph_color` was
        # correct throughout and the test above passed in all ten themes.
        #
        # So this measures the pixels.  The button is rendered over an arbitrary
        # colour no theme uses; at rest it paints no background of its own, so
        # every pixel differing from that backdrop is the mark.  A mark painted in
        # colour C and antialiased over B can only produce pixels on the line
        # between B and C, so requiring every such pixel to be a blend of the
        # backdrop and the theme's text colour accepts the right colour and
        # rejects black with no threshold to tune.  Verified: the broken version
        # deviates by 32-67 per channel and clamps its coverage to zero in eight
        # of the ten themes, meaning the pixel points away from the theme colour
        # entirely; the fixed version deviates by less than one.
        backdrop = QColor("#123456")
        window = MainWindow(FakeEngine())
        window.show()
        try:
            for theme_id, _label, _description in THEMES:
                with self.subTest(theme=theme_id):
                    window._apply_theme(theme_id)
                    self.app.processEvents()
                    button = window.help_button
                    text = QColor(button._glyph_color)
                    self._assert_only_blends_of(
                        button, backdrop, text, theme_id
                    )
        finally:
            window.close()
            self.app.processEvents()

    def _assert_only_blends_of(
        self,
        button: object,
        backdrop: QColor,
        target: QColor,
        label: str,
    ) -> None:
        """Every pixel the button painted is a blend of backdrop and target.

        A stroke or a filled mark drawn in one colour and antialiased against a
        flat background can only produce pixels on the straight line between the
        two.  A mark painted in a third colour - black, most obviously - produces
        pixels that do not lie on that line at all, whatever the theme's own
        settings say.
        """

        canvas = QPixmap(button.size())
        canvas.fill(backdrop)
        button.render(canvas)
        image = canvas.toImage()

        span = [
            target.red() - backdrop.red(),
            target.green() - backdrop.green(),
            target.blue() - backdrop.blue(),
        ]
        denominator = sum(component * component for component in span)
        painted = 0
        worst = 0
        for y in range(image.height()):
            for x in range(image.width()):
                colour = image.pixelColor(x, y)
                if colour == backdrop:
                    continue
                painted += 1
                offset = [
                    colour.red() - backdrop.red(),
                    colour.green() - backdrop.green(),
                    colour.blue() - backdrop.blue(),
                ]
                if denominator == 0:
                    coverage = 0.0
                else:
                    coverage = sum(
                        o * s for o, s in zip(offset, span)
                    ) / denominator
                    coverage = max(0.0, min(1.0, coverage))
                for observed, step in zip(offset, span):
                    worst = max(worst, abs(observed - coverage * step))

        self.assertGreater(painted, 0, f"{label}: the help mark was not painted at all")
        self.assertLessEqual(
            worst,
            _BLEND_TOLERANCE,
            f"{label}: the help mark is painted in something other than the "
            f"theme's own colour; a pixel is {worst} away from any blend of "
            f"the backdrop and {target.name()}",
        )

    def test_the_help_button_is_in_the_list_of_painted_caption_buttons(self) -> None:
        # Not a caption control - it never moves the window - but it is painted
        # the same way, so it has to be told the theme with the others.
        window = MainWindow(FakeEngine())
        try:
            self.assertIn(window.help_button, window._caption_buttons())
            self.assertEqual(len(window._caption_buttons()), 4)
        finally:
            window.close()
            self.app.processEvents()

    def test_removing_ffmpeg_is_offered_only_when_clipdock_installed_it(self) -> None:
        # `clear_cached_ffmpeg()` had been written for this and called from
        # nowhere.  It must stay limited to ClipDock's own copy: a copy on the
        # device that belongs to something else is used, but is not ours to delete.
        window = MainWindow(FakeEngine())
        try:
            action = window._remove_ffmpeg_action
            with patch("youtube_downloader.gui.main_window.installed_ffmpeg", return_value=None):
                window._refresh_help_menu()
                self.assertFalse(action.isEnabled())
            with patch(
                "youtube_downloader.gui.main_window.installed_ffmpeg",
                return_value=Path("C:/Program Files/ClipDock/ffmpeg.exe"),
            ):
                window._refresh_help_menu()
                self.assertTrue(action.isEnabled())
        finally:
            window.close()
            self.app.processEvents()

    def test_the_missing_ffmpeg_message_points_at_the_menu_that_exists(self) -> None:
        # The old text told the reader to close and reopen ClipDock, which was
        # true when there was nothing to click.  A message naming a menu item
        # that does not exist sends the user looking for something that is not
        # there, so both are asserted here: the message names "Help", and the
        # window built right there offers it.
        with patch("youtube_downloader.core.binaries.find_ffmpeg", return_value=None):
            with self.assertRaises(DependencyError) as context:
                require_ffmpeg(Path("C:/nowhere"))
        message = str(context.exception)
        self.assertIn("Help", message)
        self.assertNotIn("reopen", message.lower())

        window = MainWindow(FakeEngine())
        try:
            labels = [action.text().lstrip("&") for action in window.help_button.menu().actions()]
            self.assertIn("Check dependencies again", labels)
        finally:
            window.close()
            self.app.processEvents()

    def test_the_scroll_bar_handle_is_a_rounded_pill_inside_its_groove(self) -> None:
        # A stock scroll bar is a solid block welded to its gutter with square
        # ends.  The modern one is a capsule floating inside the gutter: inset on
        # both sides, fully rounded, and wider on hover.  All three are QSS
        # geometry, so they are checked as geometry.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            sheet = window.styleSheet()
            groove = self._rule_for(sheet, "QScrollBar:vertical")
            self.assertIn("width: 14px", groove)
            handle = self._rule_for(sheet, "QScrollBar::handle:vertical")
            margin = re.search(r"margin: (\d+)px (\d+)px", handle)
            self.assertIsNotNone(margin)
            groove_inset = int(margin.group(1))
            side_inset = int(margin.group(2))
            radius = int(re.search(r"border-radius: (\d+)px", handle).group(1))
            # Inset on both sides, so the pill never touches the groove...
            self.assertGreater(side_inset, 0)
            self.assertGreater(groove_inset, 0)
            # ...and the radius is half the pill, so both ends are round rather
            # than the handle being a rounded rectangle with square top and
            # bottom, which is what an arbitrary radius produces.
            self.assertEqual(radius * 2, 14 - side_inset * 2)
            # The hover pill is wider, and still rounded at its new width.
            hover = self._rule_for(sheet, "QScrollBar::handle:vertical:hover")
            hover_margin = re.search(r"margin: (\d+)px (\d+)px", hover)
            self.assertLess(int(hover_margin.group(2)), side_inset)
            self.assertEqual(
                int(re.search(r"border-radius: (\d+)px", hover).group(1)) * 2,
                14 - int(hover_margin.group(2)) * 2,
            )
            # A usable grab target even for a long list.
            self.assertGreaterEqual(
                int(re.search(r"min-height: (\d+)px", handle).group(1)), 40
            )
            # The horizontal bar is the same design, not an afterthought.
            self.assertIn("height: 14px", self._rule_for(sheet, "QScrollBar:horizontal"))
            self.assertIn(
                "min-width: 40px",
                self._rule_for(sheet, "QScrollBar::handle:horizontal"),
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_a_wheel_over_a_list_never_changes_the_selected_option(self) -> None:
        # Scrolling a combo box used to move its selection one step per wheel
        # notch, which on this window meant a stray scroll could change the
        # download mode, the quality, or the theme without any click at all.
        # The cursor resting on a list is a normal thing to happen while
        # scrolling the page behind it, so the value has to survive it.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            for name, combo in (
                ("mode", window.mode_combo),
                ("bitrate", window.bitrate_combo),
                ("theme", window.theme_combo),
            ):
                with self.subTest(combo=name):
                    start = combo.currentIndex()
                    centre = combo.rect().center()
                    for delta in (120, -120, 240, -240):
                        event = QWheelEvent(
                            centre,
                            combo.mapToGlobal(centre).toPointF(),
                            QPoint(0, delta),
                            QPoint(0, delta),
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase,
                            False,
                        )
                        # Sent through the application so it reaches the widget
                        # the same way a real wheel event would.
                        self.app.sendEvent(combo, event)
                        self.app.processEvents()
                    self.assertEqual(
                        combo.currentIndex(),
                        start,
                        f"scrolling changed the {name} selection",
                    )
        finally:
            window.close()
            self.app.processEvents()

    def test_every_list_shows_a_chevron_that_matches_the_theme(self) -> None:
        # The stylesheet removed the platform's own arrow, which left every list
        # looking like a plain box with no sign that it opens.  The chevron that
        # replaces it is painted rather than styled, so it has to be given the
        # theme's colours or it would keep whatever it guessed at construction.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            combos = window._selector_combos()
            # All ten drop-downs in the window, so none can be missed.
            self.assertEqual(len(combos), 10)
            for combo in combos:
                self.assertIsInstance(combo, SelectorComboBox)
            window._apply_theme("gruvbox")
            self.app.processEvents()
            colors = theme_palette("gruvbox")
            for combo in combos:
                with self.subTest(combo=combo.objectName() or type(combo).__name__):
                    self.assertEqual(
                        combo._chevron_color.name(QColor.NameFormat.HexRgb),
                        colors["text"].lower(),
                    )
            window._apply_theme("nord")
            self.app.processEvents()
            nord = theme_palette("nord")
            self.assertEqual(
                combos[0]._chevron_color.name(QColor.NameFormat.HexRgb),
                nord["text"].lower(),
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_the_cover_art_switch_only_appears_for_mp3_output(self) -> None:
        # The switch puts a thumbnail inside an MP3, so in video and subtitle
        # modes it would be a control that cannot do anything.  It is tied to
        # the same condition that reveals the bitrate picker.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            checkbox = window.embed_cover_checkbox
            audio = window.mode_combo.findData("audio")
            video = window.mode_combo.findData("video")
            subtitles = window.mode_combo.findData("subtitles")

            window.mode_combo.setCurrentIndex(audio)
            self.app.processEvents()
            self.assertTrue(checkbox.isVisibleTo(window))

            for index in (video, subtitles):
                with self.subTest(mode=window.mode_combo.itemText(index)):
                    window.mode_combo.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertFalse(checkbox.isVisibleTo(window))
        finally:
            window.close()
            self.app.processEvents()

    def test_the_retry_button_names_itself_after_the_failures(self) -> None:
        # "Retry failed" on its own said nothing about what would happen.  The
        # resting label is a plain "Retry", and it only becomes "Retry failed"
        # once a job has actually left something to retry.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            button = window.retry_button
            self.assertEqual(button.text(), "Retry")
            self.assertFalse(button.isEnabled())

            window.mode_combo.setCurrentIndex(window.mode_combo.findData("queue"))
            self.app.processEvents()
            self.assertEqual(button.text(), "Retry")
            self.assertFalse(button.isEnabled())

            result = QueueDownloadResult(
                items=(
                    QueueItemResult(url="https://www.youtube.com/watch?v=aaa", index=1, path=Path("a.mp3")),
                    QueueItemResult(url="https://www.youtube.com/watch?v=bbb", index=2, error="boom"),
                ),
            )
            window._apply_queue_results(result)
            self.assertEqual(button.text(), "Retry failed")
            self.assertTrue(button.isEnabled())
            self.assertIn("1 link", button.toolTip())

            # Two failures read as plural, so the count is never wrong.
            window._last_failed_queue = ("https://www.youtube.com/watch?v=bbb", "https://youtu.be/ccc")
            window._refresh_retry_button()
            self.assertIn("2 links", button.toolTip())

            # A playlist retry names videos rather than links.
            window._last_failed_queue = ()
            window.mode_combo.setCurrentIndex(window.mode_combo.findData("playlist"))
            self.app.processEvents()
            self.assertEqual(button.text(), "Retry")
            window._last_failed_playlist = ("vid1", "vid2")
            window._refresh_retry_button()
            self.assertEqual(button.text(), "Retry failed")
            self.assertIn("2 videos", button.toolTip())
        finally:
            window.close()
            self.app.processEvents()

    def test_a_retry_asks_the_engine_for_only_the_failed_videos(self) -> None:
        # The retry is wired to the ids the engine recorded, not to the failure
        # titles: a playlist can hold the same title twice, and matching on it
        # would retry the wrong video.
        window = MainWindow(FakeEngine())
        window.show()
        self.app.processEvents()
        try:
            window.playlist_info = PlaylistInfo(
                "list",
                "A playlist",
                (),
                (),
                "https://www.youtube.com/playlist?list=list",
            )
            window.mode_combo.setCurrentIndex(window.mode_combo.findData("playlist"))
            self.app.processEvents()
            # Audio, so the retry does not also need a quality chosen, which is
            # not what this test is about.
            window.playlist_media_combo.setCurrentIndex(
                window.playlist_media_combo.findData("audio")
            )
            self.app.processEvents()
            window._last_failed_playlist = ("vid1", "vid3")

            captured: dict[str, object] = {}

            class RecordingEngine(FakeEngine):
                def download_playlist(self, request, *, progress=None, cancel_check=None):
                    captured["ids"] = request.selected_video_ids
                    return PlaylistDownloadResult(paths=(), total=2)

            window.engine = RecordingEngine()
            window._retry_failed_items()
            self._wait_for_job(window)
            self.assertEqual(captured.get("ids"), ("vid1", "vid3"))
        finally:
            window.close()
            self.app.processEvents()

    def _pause_window(self, engine) -> MainWindow:
        """A shown window whose job is stopped even when the test fails.

        The pause fakes block until they are cancelled -- that is what makes
        them pauseable at all -- so a test that fails *while* one is running
        would leave the worker spinning and the suite would hang instead of
        reporting the failure.  `addCleanup` covers every path out of the test,
        including an assertion raised before anything was stopped, which a
        `finally` around the body only covers if the body reached its `try`.
        """
        window = MainWindow(engine)
        window.show()
        self.app.processEvents()

        def stop() -> None:
            if window.controller.is_running:
                window.controller.cancel()
                self._wait_for_job(window)
            window.close()
            self.app.processEvents()

        self.addCleanup(stop)
        return window

    def test_pause_stops_the_download_and_offers_resume_only_for_what_it_wrote(self) -> None:
        # The rule for this control is that it is not there until it is real,
        # and "real" is doing a lot of work: the folder can hold an earlier
        # session's crash, and offering that alongside the file this job was
        # writing would be the wrong count on a new button.
        engine = PausingEngine()
        window = self._pause_window(engine)
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "last-week.mp4.part").write_bytes(b"z" * 4096)
            window.destination_edit.setText(directory)

            window._start_download()
            self.assertTrue(window.controller.is_running)
            self.assertTrue(
                window.pause_button.isEnabled(),
                "a running download is the one thing Pause exists for",
            )
            self.assertFalse(
                window.resume_button.isVisibleTo(window),
                "nothing has been stopped yet, so there is nothing to resume",
            )

            window.pause_button.click()
            self._wait_for_job(window)

            self.assertEqual(engine.downloads_started, 1)
            self.assertIn("Paused", window.status_label.text())
            self.assertTrue(window.resume_button.isVisibleTo(window))
            self.assertTrue(window.resume_button.isEnabled())
            self.assertIn("1 download", window.resume_button.toolTip())
            self.assertNotIn("2 downloads", window.resume_button.toolTip())
            self.assertTrue((Path(directory) / "half-done.mp4.part").is_file())

    def test_a_cancel_is_not_a_pause_and_offers_nothing_to_resume(self) -> None:
        # Both stops end as the same cancellation as far as the worker is
        # concerned, and only the intent differs -- so the intent is what has to
        # be recorded, or Cancel would offer to continue a job the person just
        # said they did not want.
        engine = PausingEngine()
        window = self._pause_window(engine)
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            self.assertTrue(window.controller.is_running)

            window.cancel_button.click()
            self._wait_for_job(window)

            self.assertEqual(engine.downloads_started, 1)
            self.assertIn("Cancelled", window.status_label.text())
            self.assertFalse(window.resume_button.isVisibleTo(window))
            self.assertTrue(
                (Path(directory) / "half-done.mp4.part").is_file(),
                "Cancel keeps the pieces too; it just does not offer to continue them",
            )

    def test_a_pause_with_nothing_on_disk_offers_nothing_to_resume(self) -> None:
        # Two ways for a stop to have left nothing worth continuing, and a
        # leftover from an earlier session sitting in the same folder for each
        # of them: the snapshot is what stops that leftover being offered as
        # though this job had produced it.
        for writes, marker in (("nothing", "no file at all"), ("state", "only the .ytdl state file")):
            with self.subTest(writes=writes, marker=marker):
                engine = PausingEngine(writes=writes)
                window = self._pause_window(engine)
                window.url_edit.setText("https://youtu.be/id")
                window._fetch_details()
                self._wait_for_job(window)
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / "last-week.mp4.part").write_bytes(b"z" * 4096)
                    window.destination_edit.setText(directory)
                    window._start_download()
                    window.pause_button.click()
                    self._wait_for_job(window)

                    self.assertEqual(engine.downloads_started, 1)
                    self.assertFalse(
                        window.resume_button.isVisibleTo(window),
                        f"a stopped download that left {marker} is not something to continue",
                    )
                    self.assertFalse(window.resume_button.isEnabled())

    def test_resume_re_runs_the_job_that_was_paused(self) -> None:
        # The stored request is used rather than the settings now on screen,
        # because it is the only record a re-run can drive; `history.json`
        # writes what a job was made with too, and nothing reads it back.
        # A second run is allowed to finish so the whole round trip is visible:
        # stopped, offered, re-run, finished.
        engine = PausingEngine(stops=1)
        window = self._pause_window(engine)
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            window.pause_button.click()
            self._wait_for_job(window)
            self.assertTrue(window.resume_button.isVisibleTo(window))

            window.resume_button.click()
            self.assertTrue(window.controller.is_running)
            self.assertFalse(
                window.resume_button.isVisibleTo(window),
                "a running job has nothing to resume while it is running",
            )
            self._wait_for_job(window)

            self.assertEqual(engine.downloads_started, 2, "Resume did not re-run the job")
            self.assertTrue((Path(directory) / "half-done.mp4").is_file())
            self.assertFalse(
                window.resume_button.isVisibleTo(window),
                "a finished download has nothing left to resume",
            )

    def test_pause_is_offered_only_for_a_download_job(self) -> None:
        # Reading a video's details is a job like any other as far as the worker
        # is concerned, and it is the one thing there is nothing to pause: no
        # file is being written, so a Pause there would be a second Cancel
        # wearing a different label.
        class BlockingProbeEngine(FakeEngine):
            def probe(self, url, *, progress=None, cancel_check=None):
                while not cancel_check():
                    time.sleep(0.005)
                raise CancelledError()

        window = self._pause_window(BlockingProbeEngine())
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self.assertTrue(window.controller.is_running)
        self.assertFalse(window.pause_button.isEnabled())
        self.assertFalse(window.resume_button.isVisibleTo(window))

        window.cancel_button.click()
        self._wait_for_job(window)
        self.assertFalse(window.pause_button.isEnabled())

    def test_pause_does_not_claim_to_be_a_true_pause(self) -> None:
        # yt-dlp exposes no pause primitive, so what ships is a graceful stop
        # that keeps the pieces -- and whether the continuation then picks up at
        # the byte it reached has never been measured against the live service.
        # The tooltip is the only thing between a reader and that assumption, so
        # the wording is asserted rather than left to be reworded later.
        window = self._pause_window(FakeEngine())
        tooltip = window.pause_button.toolTip()
        self.assertIn("Not a true pause", tooltip)
        self.assertIn("keep what has arrived", tooltip)
        for claim in ("where it left off", "exactly where it stopped", "the same byte it"):
            self.assertNotIn(claim, tooltip)

    def test_the_window_carries_the_program_icon(self) -> None:
        # Windows shows a generic Python icon for a taskbar button or an Alt+Tab
        # entry unless the program sets one, and it reads as "not a real program".
        icon_path = find_icon()
        self.assertIsNotNone(icon_path, "the application icon was not found")
        app = self.app
        icon = QIcon(str(icon_path))
        self.assertFalse(icon.isNull(), "the application icon could not be loaded")
        app.setWindowIcon(icon)
        window = MainWindow(FakeEngine())
        window.setWindowIcon(icon)
        window.show()
        self.app.processEvents()
        try:
            self.assertFalse(window.windowIcon().isNull())
            # The title bar caption has to agree with the window title, or the
            # taskbar tooltip and the drawn caption disagree.
            self.assertEqual(window.title_bar_caption.text(), window.windowTitle())
        finally:
            window.close()
            self.app.processEvents()

    def test_playlist_row_sizes_follow_the_stream_preference(self) -> None:
        window = MainWindow(FakeEngine())
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("playlist"))
        window.url_edit.setText("https://www.youtube.com/playlist?list=PL123")
        window._fetch_details()
        self._wait_for_job(window)

        # The playlist's 1080p rows carry a leaner alternative in this fixture,
        # so their per-video number has to move with the preference.
        self.assertEqual(
            window.playlist_table.horizontalHeaderItem(PLAYLIST_COLUMN_SIZE).text(),
            "Size at MP4",
        )
        best_row = window.playlist_table.item(0, PLAYLIST_COLUMN_SIZE).text()
        self.assertIn(format_size(12_000_000), best_row)

        index = window.stream_preference_combo.findData(StreamPreference.SMALLER_FILE.value)
        window.stream_preference_combo.setCurrentIndex(index)
        self.app.processEvents()
        self.assertEqual(
            window.playlist_table.horizontalHeaderItem(PLAYLIST_COLUMN_SIZE).text(),
            "Size at MP4 (smaller file)",
        )
        leaner = window.playlist_table.item(0, PLAYLIST_COLUMN_SIZE).text()
        self.assertIn(format_size(7_000_000), leaner)
        self.assertNotIn(format_size(12_000_000), leaner)
        window.close()
        self.app.processEvents()

    # ------------------------------------------------------------------ history

    def _open_window(self, engine) -> MainWindow:
        """A shown window whose worker and history window are both closed for us.

        Registered before anything is started, so a failure part-way through
        cannot leave a thread spinning and the suite hanging instead of
        reporting - the same reason `_pause_window` cleans up the way it does.
        """
        window = MainWindow(engine)
        window.show()
        self.app.processEvents()

        def close() -> None:
            if window.controller.is_running:
                window.controller.cancel()
                self._wait_for_job(window)
            if window._history_window is not None:
                window._history_window.close()
            window.close()
            self.app.processEvents()

        self.addCleanup(close)
        return window

    def _one_finished_download(
        self,
        window: MainWindow,
        directory: str,
        url: str = "https://youtu.be/id",
    ) -> None:
        window.url_edit.setText(url)
        window._fetch_details()
        self._wait_for_job(window)
        window.destination_edit.setText(directory)
        window._start_download()
        self._wait_for_job(window)

    def test_a_finished_download_is_written_to_the_history_file(self) -> None:
        window = self._open_window(FakeEngine())
        with tempfile.TemporaryDirectory() as directory:
            self._one_finished_download(window, directory)

            jobs = history.read_history(self.history_path)
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job["mode"], "video")
            self.assertEqual(job["outcome"], history.OUTCOME_COMPLETE)
            self.assertEqual(job["succeeded"], 1)
            self.assertEqual(job["failed"], 0)
            self.assertEqual(job["folder"], str(Path(directory)))
            # The header of a job says when it ran, and there is no second
            # chance to ask once the job is over.
            self.assertTrue(job["started"])

            (item,) = job["items"]
            self.assertEqual(item["title"], "A test video")
            self.assertEqual(item["destination"], str(Path(directory) / "downloaded.mp4"))
            self.assertEqual(item["url"], "https://youtu.be/id")
            self.assertEqual(item["outcome"], history.ITEM_SAVED)

            # The choices the job ran with are kept for a later "get it again",
            # even though the window never shows them.
            self.assertEqual(job["request"]["output_dir"], directory)
            self.assertEqual(job["request"]["mode"], "video")
            self.assertNotIn("info", job["request"])

    def test_reading_a_videos_details_is_not_recorded_as_a_download(self) -> None:
        # A probe produces details, not files.  A history that listed links the
        # person never got would answer "what did I get" with things they do not
        # have, so the guard is `_active_request`, which only a real job sets.
        window = self._open_window(FakeEngine())
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)

        self.assertEqual(history.read_history(self.history_path), [])

    def test_an_update_check_is_never_written_into_the_history(self) -> None:
        window = self._open_window(FakeEngine())
        with tempfile.TemporaryDirectory() as directory:
            self._one_finished_download(window, directory)
            self.assertEqual(len(history.read_history(self.history_path)), 1)

            with patch("youtube_downloader.gui.main_window.QMessageBox"):
                window._on_job_succeeded(
                    UpdateCheck(status="current", current="0.2.0", latest=None)
                )

            # Not added, and the job that had just finished is not re-recorded
            # or counted twice by it either.
            self.assertEqual(len(history.read_history(self.history_path)), 1)

    def test_a_failure_after_a_download_is_not_filed_as_a_second_job(self) -> None:
        window = self._open_window(FakeEngine())
        with tempfile.TemporaryDirectory() as directory:
            self._one_finished_download(window, directory)
            self.assertEqual(len(history.read_history(self.history_path)), 1)

            # The next thing to fail is a probe, which has no request of its
            # own.  Without the guard this would be filed as another attempt at
            # the download that just finished, and the history would claim two
            # jobs where one happened.
            with patch("youtube_downloader.gui.main_window.QMessageBox"):
                window._on_job_failed(RuntimeError("no network"))

            self.assertEqual(len(history.read_history(self.history_path)), 1)

    def test_a_paused_download_is_recorded_as_a_stop_and_not_a_failure(self) -> None:
        engine = PausingEngine()
        window = self._pause_window(engine)
        window.url_edit.setText("https://youtu.be/id")
        window._fetch_details()
        self._wait_for_job(window)
        with tempfile.TemporaryDirectory() as directory:
            window.destination_edit.setText(directory)
            window._start_download()
            # PausingEngine blocks until it is cancelled, which is what makes it
            # pauseable at all: there is nothing to wait for here.
            self.assertTrue(window.controller.is_running)
            window.pause_button.click()
            self._wait_for_job(window)

        jobs = history.read_history(self.history_path)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["outcome"], history.OUTCOME_PAUSED)
        self.assertEqual(jobs[0]["succeeded"], 0)
        self.assertEqual(jobs[0]["failed"], 0)
        # The stop is written before Resume is offered, so a person who closes
        # the window instead of continuing still has the record of it.
        (item,) = jobs[0]["items"]
        self.assertEqual(item["outcome"], history.ITEM_STOPPED)

    def test_the_history_button_sits_with_the_action_buttons_and_always_works(self) -> None:
        window = self._open_window(FakeEngine())
        container = window.retry_button.parentWidget()
        self.assertIs(window.history_button.parentWidget(), container)
        self.assertTrue(window.history_button.isVisibleTo(window))

        # Found in the layout rather than assumed from the source, because the
        # point of the decision is where it actually ends up: beside Retry, on
        # the left of the stretch, with the other action buttons and not with
        # Download and Cancel on the right.
        row = None
        layout = container.layout()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            child = item.layout() if item is not None else None
            if child is not None and child.indexOf(window.retry_button) >= 0:
                row = child
                break
        self.assertIsNotNone(row)
        self.assertEqual(row.indexOf(window.history_button), row.indexOf(window.resume_button) + 1)
        self.assertLess(row.indexOf(window.history_button), row.indexOf(window.pause_button))

        # Retry is dead until something has failed.  History answers on the very
        # first launch, when the answer is "nothing yet".
        self.assertFalse(window.retry_button.isEnabled())
        self.assertTrue(window.history_button.isEnabled())

    def test_the_history_window_groups_a_job_and_lists_its_files_underneath(self) -> None:
        window = self._open_window(FakeEngine())
        with tempfile.TemporaryDirectory() as directory:
            self._one_finished_download(window, directory)
            window._show_history()
            view = window._history_window

            self.assertEqual(view.tree.topLevelItemCount(), 1)
            top = view.tree.topLevelItem(0)
            self.assertIn("Video", top.text(1))
            self.assertIn("https://youtu.be/id", top.text(1))
            self.assertIn("Complete", top.text(2))
            self.assertIn("1 file saved", top.text(2))
            self.assertTrue(top.text(0), "the job header says when it ran")
            self.assertEqual(top.text(3), str(Path(directory)))

            self.assertEqual(top.childCount(), 1)
            child = top.child(0)
            self.assertEqual(child.text(1), "A test video")
            self.assertIn("Saved", child.text(2))
            self.assertEqual(child.text(3), str(Path(directory) / "downloaded.mp4"))

            self.assertEqual(view.summary_label.text(), "1 job recorded · newest first")
            self.assertTrue(view.tree.isVisible())
            self.assertFalse(view.empty_label.isVisible())

    def test_the_newest_job_is_open_and_older_ones_are_folded(self) -> None:
        window = self._open_window(FakeEngine())
        with tempfile.TemporaryDirectory() as directory:
            for url in ("https://youtu.be/one", "https://youtu.be/two"):
                self._one_finished_download(window, directory, url=url)
            window._show_history()
            view = window._history_window

            self.assertEqual(view.tree.topLevelItemCount(), 2)
            # The newest has a question attached to it - "what did I just get"
            # - and is open.  An old one is folded, so a history nobody is
            # looking at is not drawn in full every time it is opened.
            self.assertTrue(view.tree.topLevelItem(0).isExpanded())
            self.assertFalse(view.tree.topLevelItem(1).isExpanded())

    def test_a_row_offers_its_folder_and_a_source_link_that_must_be_https(self) -> None:
        # Written straight into the file: this is a rule about what a row is
        # willing to do, and it holds for anything a hand-edited history can
        # contain as much as for anything the program wrote itself.  `http` is
        # recorded last so that it is the newest job, and therefore the first
        # row the window shows.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        folder.mkdir(exist_ok=True)
        for marker, url in (("https", "https://secure.example/watch"), ("http", "http://insecure.example/watch")):
            history.record_job(
                {
                    "started": f"2026-10-07T09:{marker[:2]}:00",
                    "finished": "2026-10-07T09:10:00",
                    "mode": "video",
                    "requested": url,
                    "folder": str(folder),
                    "outcome": "complete",
                    "reason": "",
                    "succeeded": 1,
                    "failed": 0,
                    "total": 1,
                    "items": [
                        {
                            "title": marker,
                            "destination": str(folder / f"{marker}.mp4"),
                            "outcome": "saved",
                            "reason": "",
                            "url": url,
                        }
                    ],
                    "request": {},
                },
                self.history_path,
            )

        window = self._open_window(FakeEngine())
        window._show_history()
        view = window._history_window
        self.assertEqual(view.tree.topLevelItemCount(), 2)

        newest, older = view.tree.topLevelItem(0), view.tree.topLevelItem(1)
        self.assertEqual(newest.child(0).text(1), "http")

        view.tree.setCurrentItem(newest.child(0))
        self.assertTrue(view.folder_button.isEnabled())
        self.assertFalse(
            view.link_button.isEnabled(),
            "a link handed to the browser on the user's behalf is https or nothing",
        )

        view.tree.setCurrentItem(older.child(0))
        self.assertTrue(view.folder_button.isEnabled())
        self.assertTrue(view.link_button.isEnabled())

        with patch("youtube_downloader.gui.history_window.QDesktopServices") as desktop:
            view.open_folder()
        desktop.openUrl.assert_called_once()

    def test_clearing_the_history_asks_first_and_then_empties_the_file(self) -> None:
        window = self._open_window(FakeEngine())
        history.record_job({"started": "2026-10-07T09:00:00", "items": []}, self.history_path)
        window._show_history()
        view = window._history_window
        self.assertEqual(view.tree.topLevelItemCount(), 1)

        # Asked for because it cannot be undone.
        with patch("youtube_downloader.gui.history_window.QMessageBox") as boxes:
            boxes.question.return_value = boxes.StandardButton.No
            view._clear()
        self.assertEqual(len(history.read_history(self.history_path)), 1)
        self.assertEqual(view.tree.topLevelItemCount(), 1)

        with patch("youtube_downloader.gui.history_window.QMessageBox") as boxes:
            boxes.question.return_value = boxes.StandardButton.Yes
            view._clear()
        self.assertEqual(history.read_history(self.history_path), [])
        self.assertEqual(view.tree.topLevelItemCount(), 0)
        self.assertTrue(view.empty_label.isVisible())
        self.assertFalse(view.clear_button.isEnabled())

    def test_closing_the_main_window_closes_the_history_too(self) -> None:
        # A child left open keeps the application running after the only window
        # anybody is using has gone.
        window = self._open_window(FakeEngine())
        window._show_history()
        view = window._history_window
        self.app.processEvents()
        self.assertTrue(view.isVisible())

        window.close()
        self.app.processEvents()
        self.assertFalse(view.isVisible())

    @staticmethod
    def _wait_for_job(window: MainWindow) -> None:
        for _ in range(100):
            QTest.qWait(50)
            QApplication.instance().processEvents()
            if not window.controller.is_running:
                QTest.qWait(10)
                QApplication.instance().processEvents()
                return
        raise AssertionError("background GUI job did not finish")


if __name__ == "__main__":
    unittest.main()
