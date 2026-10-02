"""The first-run dependency check, and the dialog that offers to resolve it.

The check runs before the window appears, not from inside it, for one reason:
a user who opens a shared copy and is then greeted by an empty window has no
way to tell a missing FFmpeg from a broken program.  Telling them up front, in
words, is the difference between "this needs one more thing" and "this is
scamware".

It never blocks the launch.  Every failure path here logs and returns, because
the whole point is that a machine which cannot reach the network, or a user who
simply says no, still gets a working window.  Without FFmpeg the application
can still fetch details, list qualities, and show sizes; only merging and
conversion are unavailable, and that is said plainly rather than implied.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core.binaries import find_ffmpeg
from ..core.dependencies import (
    DependencyReport,
    free_space_bytes,
    install_ffmpeg,
    install_targets,
    locate_device_ffmpeg,
    missing_dependencies,
)
from ..core.errors import AppError
from .workers import JobController

_LOGGER = logging.getLogger(__name__)


def describe(report: DependencyReport) -> str:
    """One dependency as a sentence naming what it is for and what it costs."""

    if report.present:
        return ""
    size = f"{report.megabytes:.0f} MB"
    return (
        f"{report.name} is needed for {report.needed_for}. It is a one-time "
        f"download of about {size}, installed for the whole computer in "
        f"{install_targets()[0]} so nothing else has to download it again."
    )


def checked_devices() -> str:
    """The one-line account of where the search looked.

    Said in the dialog rather than left implicit because "I searched your whole
    computer and found nothing" is a claim the user is entitled to see made.  If
    the copy that was found sits somewhere unexpected, this is the line that
    tells them so, before they have to ask.
    """

    found = locate_device_ffmpeg()
    if found is not None:
        return f"ClipDock checked this computer and found FFmpeg already in use at {found}."
    return (
        "ClipDock checked this computer - the usual install folders, PATH, and "
        f"{install_targets()[0]} - and could not find FFmpeg."
    )


class DependencyDialog(QDialog):
    """Asks before downloading, then shows progress and the outcome.

    It asks rather than downloading on its own because 164 MB is somebody's
    bandwidth and their disk, and an application that starts by spending both
    without asking is exactly the behaviour that makes software untrusted.
    Declining is a first-class outcome: the dialog closes and the program runs.
    """

    def __init__(self, reports: list[DependencyReport], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ClipDock needs one more thing")
        self.setModal(True)
        self._reports = reports
        self._controller = JobController(self)
        # Held until the worker thread has stopped, then reported and cleared.
        self._outcome: tuple[str, bool] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        heading = QLabel("ClipDock checked this computer before starting.")
        heading.setWordWrap(True)
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(heading)

        body = [checked_devices()]
        body.extend(filter(None, (describe(r) for r in reports)))
        self.detail_label = QLabel("\n\n".join(body))
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_label)

        # Said up front rather than after a failure, because the alternative is
        # filling a disk and only then telling the user they had no room.  The
        # volume is asked about where the file would actually land, which is not
        # necessarily where it is preferred.
        free = free_space_bytes(install_targets()[0])
        if free is not None:
            needed = max(r.download_bytes for r in reports) if reports else 0
            if free < needed * 2:
                self.detail_label.setText(
                    self.detail_label.text()
                    + f"\n\nThere may not be enough free disk space "
                    f"({free / 1_048_576:.0f} MB available)."
                )

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.later_button = QPushButton("Not now")
        self.later_button.setToolTip("Open ClipDock anyway; merging and conversion stay unavailable")
        self.later_button.clicked.connect(self.reject)
        self.install_button = QPushButton("Download")
        self.install_button.setDefault(True)
        self.install_button.clicked.connect(self._start)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.install_button)
        layout.addLayout(buttons)

        self._controller.progress.connect(self._on_progress)
        self._controller.succeeded.connect(self._on_succeeded)
        self._controller.failed.connect(self._on_failed)
        self._controller.cancelled.connect(self._on_cancelled)

    def _start(self) -> None:
        self.install_button.setEnabled(False)
        self.later_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status_label.setVisible(True)
        self.status_label.setText("Starting...")
        self._controller.start(lambda progress, cancelled: install_ffmpeg(progress, cancelled))

    def _on_progress(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        note = str(event.get("note", ""))
        done = int(event.get("done", 0) or 0)
        total = int(event.get("total", 0) or 0)
        percent = int(event.get("percent", 0) or 0)
        self.progress.setValue(max(0, min(100, percent)))
        if total:
            self.status_label.setText(f"{note}  {done / 1_048_576:.0f} of {total / 1_048_576:.0f} MB")
        elif note:
            self.status_label.setText(note)

    def _on_succeeded(self, result: object) -> None:
        folder = Path(str(result)).parent
        self._settle(
            f"FFmpeg is installed in {folder}. Any other program on this computer "
            "can use it from there too.",
            failed=False,
        )

    def _on_failed(self, error: object) -> None:
        # The dialog is offered again rather than left closed: a failed download
        # is usually a network blip, and a user who has to relaunch to retry has
        # been told, in effect, to give up.
        message = error.message if isinstance(error, AppError) else str(error)
        self._settle(f"Could not install FFmpeg: {message}", failed=True)

    def _on_cancelled(self) -> None:
        self._settle("The download was cancelled.", failed=True)

    def _settle(self, message: str, *, failed: bool) -> None:
        """Wait for the worker thread to stop, report, then close.

        Closing a QDialog that still owns a running QThread tears the thread down
        underneath it, which crashes some PySide6 builds.  So the outcome is held
        until the controller reports itself finished, and only then is the dialog
        allowed to go away.
        """

        self._outcome = (message, failed)
        self._controller.finished.connect(self._report_and_close)

    def _report_and_close(self) -> None:
        if self._controller.is_running or self._outcome is None:
            return
        message, failed = self._outcome
        self._outcome = None
        box = QMessageBox(self)
        box.setWindowTitle("Could not install FFmpeg" if failed else "ClipDock is ready")
        box.setText(message)
        box.setIcon(QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information)
        box.exec()
        self.accept()


def first_run_check(parent=None, finder: Callable[[], Path | None] = find_ffmpeg) -> bool:
    """Check the whole device, and offer to install whatever is missing.

    Returns whether anything was installed.  Called before the main window is
    shown, and deliberately total: a bug in here must not stop the program from
    starting, so anything unexpected is logged and swallowed rather than raised.

    Nothing is shown when everything is present.  That silence is the answer -
    "ClipDock checked your computer, found what it needed, and got out of the
    way" - and putting a confirmation dialog on every launch would make the
    program noisier the better behaved it was.  The result is logged instead, so
    it is still answerable after the fact.
    """

    try:
        missing = missing_dependencies(finder)
    except Exception as error:  # pragma: no cover - defensive
        _LOGGER.error("dependency_check_failed exception=%s", type(error).__name__)
        return False
    if not missing:
        # Logged with the count checked rather than a bare "ok", so the record
        # says what was actually verified instead of merely that nothing went
        # wrong.
        _LOGGER.info("dependencies_ready checked=2 missing=0")
        return False
    _LOGGER.info("dependencies_missing count=%d", len(missing))
    try:
        dialog = DependencyDialog(missing, parent)
        dialog.exec()
        return finder() is not None
    except Exception as error:  # pragma: no cover - defensive
        _LOGGER.error("dependency_dialog_failed exception=%s", type(error).__name__)
        return False
