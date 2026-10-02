from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from youtube_downloader.core.logging_setup import configure_logging


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
