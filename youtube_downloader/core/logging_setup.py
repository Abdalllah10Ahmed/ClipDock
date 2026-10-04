from __future__ import annotations

import logging
import os
import queue
from dataclasses import dataclass
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

from .errors import classify_yt_dlp_message


@dataclass
class LoggingHandle:
    logger: logging.Logger
    listener: QueueListener
    queue_handler: QueueHandler
    file_handler: RotatingFileHandler
    queue: queue.Queue[Any]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.queue_handler in self.logger.handlers:
            self.logger.removeHandler(self.queue_handler)
        self.listener.stop()
        self.file_handler.close()


_active_handle: LoggingHandle | None = None


def default_log_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ClipDock" / "logs"
    return Path.home() / "AppData" / "Local" / "ClipDock" / "logs"


def configure_logging(log_directory: Path | None = None) -> LoggingHandle:
    global _active_handle
    if _active_handle is not None:
        _active_handle.close()
        _active_handle = None

    directory = log_directory or default_log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "app.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    record_queue: queue.Queue[Any] = queue.Queue(-1)
    queue_handler = QueueHandler(record_queue)
    listener = QueueListener(record_queue, file_handler, respect_handler_level=True)
    listener.start()

    logger = logging.getLogger("youtube_downloader")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(queue_handler)

    handle = LoggingHandle(logger, listener, queue_handler, file_handler, record_queue)
    _active_handle = handle
    return handle


def get_logger() -> logging.Logger:
    return logging.getLogger("youtube_downloader")


class SafeYtDlpLogger:
    """ yt-dlp logger that keeps a classified reason and discards the raw text.

    yt-dlp messages carry video URLs, titles and file paths, so the text is
    never written to the log -- that part is deliberate.  What was lost with it
    was the *kind* of failure: a rate limit, a dropped connection and a removed
    video all logged the same bare label, which is the one distinction a
    throttled caption run needs in order to be diagnosable at all.  Each warning
    and error is therefore reduced by :func:`~.errors.classify_yt_dlp_message`
    to a single token from a closed vocabulary, which is the same decision made
    for the user-facing message, so the two can never disagree.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger()

    def debug(self, message: str) -> None:
        self._logger.debug("yt_dlp_debug")

    def info(self, message: str) -> None:
        self._logger.info("yt_dlp_info")

    def warning(self, message: str) -> None:
        self._logger.warning("yt_dlp_warning reason=%s", classify_yt_dlp_message(message))

    def error(self, message: str) -> None:
        self._logger.error("yt_dlp_error reason=%s", classify_yt_dlp_message(message))
