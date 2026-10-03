from __future__ import annotations

import dataclasses
import json
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPalette, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from youtube_downloader.core.binaries import find_icon, require_ffmpeg
from youtube_downloader.core.errors import DependencyError
from youtube_downloader.core.updates import UpdateCheck

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
        self.assertIn("only one MP4 stream", window.stream_preference_combo.toolTip())
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
            window._report_update = reported.append
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
