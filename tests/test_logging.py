from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from youtube_downloader.core.errors import classify_yt_dlp_message
from youtube_downloader.core.logging_setup import SafeYtDlpLogger, configure_logging

# What yt-dlp actually said during the 25-language run that wrote 0 of 25 files.
RATE_LIMIT_TEXT = (
    "ERROR: Unable to download video subtitles for 'aa': HTTP Error 429: Too Many Requests"
)


class ClassifiedReasonTests(unittest.TestCase):
    def test_a_rate_limit_is_told_apart_from_a_dropped_connection(self) -> None:
        self.assertEqual(classify_yt_dlp_message(RATE_LIMIT_TEXT), "rate_limited")
        self.assertEqual(
            classify_yt_dlp_message(
                "Unable to download video page: HTTP Error 503: Service Unavailable"
            ),
            "network_failure",
        )

    def test_a_removed_video_is_not_mistaken_for_a_network_fault(self) -> None:
        self.assertEqual(
            classify_yt_dlp_message("ERROR: Video unavailable. This video is private"),
            "private",
        )
        self.assertEqual(
            classify_yt_dlp_message("ERROR: [youtube] abc: Video unavailable"),
            "unavailable",
        )

    def test_ordinary_progress_and_failure_text_classifies_as_unrecognised(self) -> None:
        # The token is a closed set.  A message that names none of the known
        # causes must say so rather than borrow a reason that would then be
        # believed.
        self.assertEqual(
            classify_yt_dlp_message("[download] Destination: Example video.f137.mp4"), "unrecognised"
        )
        self.assertEqual(classify_yt_dlp_message(""), "unrecognised")

    def test_the_classification_never_carries_any_of_the_original_text(self) -> None:
        message = (
            "ERROR: [youtube] dQw4w9WgXcQ: Unable to download video subtitles: "
            "HTTP Error 429: Too Many Requests (https://www.youtube.com/watch?v=dQw4w9WgXcQ)"
        )
        reason = classify_yt_dlp_message(message)
        self.assertEqual(reason, "rate_limited")
        for fragment in ("youtube.com", "dQw4w9WgXcQ", "429:"):
            self.assertNotIn(fragment, reason)


class SafeLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(inner_self, record: logging.LogRecord) -> None:
                self.records.append(record)

        self.logger = logging.getLogger(f"test.safe.{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(_Capture())
        self.ydl_logger = SafeYtDlpLogger(self.logger)

    def test_a_warning_records_its_kind_without_the_text_that_names_the_video(self) -> None:
        self.ydl_logger.warning(
            "WARNING: unable to download video subtitles for 'aa': "
            "HTTP Error 429: Too Many Requests (https://www.youtube.com/watch?v=dQw4w9WgXcQ)"
        )
        self.assertEqual(len(self.records), 1)
        message = self.records[0].getMessage()
        self.assertIn("yt_dlp_warning", message)
        self.assertIn("reason=rate_limited", message)
        self.assertNotIn("dQw4w9WgXcQ", message)
        self.assertNotIn("https://", message)

    def test_an_unclassifiable_error_still_records_that_an_error_happened(self) -> None:
        self.ydl_logger.error("ERROR: something nobody has a word for yet")
        message = self.records[0].getMessage()
        self.assertIn("yt_dlp_error", message)
        self.assertIn("reason=unrecognised", message)
        self.assertNotIn("nobody has a word", message)

    def test_chatter_stays_unlabelled(self) -> None:
        self.ydl_logger.info("[info] dQw4w9WgXcQ: Downloading subtitles: en")
        self.ydl_logger.debug("[debug] something")
        messages = [record.getMessage() for record in self.records]
        self.assertEqual(messages, ["yt_dlp_info", "yt_dlp_debug"])


class LoggingTests(unittest.TestCase):
    def test_logging_is_rotating_and_does_not_write_raw_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handle = configure_logging(Path(directory))
            handle.logger.info("operation_started mode=video")
            handle.close()
            content = (Path(directory) / "app.log").read_text(encoding="utf-8")
            self.assertIn("operation_started mode=video", content)
            self.assertNotIn("https://", content)


if __name__ == "__main__":
    unittest.main()
