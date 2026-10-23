from __future__ import annotations

import errno
import logging
import tempfile
import unittest
from pathlib import Path

from youtube_downloader.core.errors import (
    classify_yt_dlp_message,
    friendly_error,
    safe_log_error,
)
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

    def test_a_refused_media_request_is_explained_as_a_stale_link(self) -> None:
        mapped = friendly_error(
            RuntimeError("unable to download video data: HTTP Error 403: Forbidden")
        )
        self.assertEqual(mapped.code, "download_failed")
        self.assertIn("403", mapped.message)
        self.assertIn("Fetch the video details again", mapped.message)

    def test_a_reset_socket_is_a_network_fault_not_a_disk_problem(self) -> None:
        # `ConnectionResetError` has no space in its class name, so the text
        # vocabulary does not see it; without the errno branch it was reported
        # as "the file could not be written".
        mapped = friendly_error(OSError(errno.ECONNRESET, "socket closed"))
        self.assertEqual(mapped.code, "network_failure")


class OperationFailureLogTests(unittest.TestCase):
    def test_a_failed_operation_records_the_closed_set_reason(self) -> None:
        logger = logging.getLogger("test.safe.operation")
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        with self.assertLogs(logger, level="ERROR") as caught:
            safe_log_error(
                logger,
                RuntimeError(
                    "Unable to download webpage: HTTP Error 503: Service Unavailable "
                    "(https://www.youtube.com/watch?v=dQw4w9WgXcQ)"
                ),
                operation="probe",
            )
        line = caught.output[0]
        self.assertIn("operation=probe", line)
        self.assertIn("category=network_failure", line)
        self.assertIn("reason=network_failure", line)
        self.assertIn("exception=RuntimeError", line)
        self.assertNotIn("dQw4w9WgXcQ", line)
        self.assertNotIn("https://", line)


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
