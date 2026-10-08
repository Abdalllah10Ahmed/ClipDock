from __future__ import annotations

from threading import Event
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..core.errors import AppError, CancelledError, friendly_error
from ..core.logging_setup import get_logger


class JobWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    started = Signal()

    def __init__(self, operation: Callable[[Callable[[object], None], Callable[[], bool]], Any]) -> None:
        super().__init__()
        self._operation = operation
        self._cancel_event = Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        thread = self.thread()
        if thread and thread.isRunning():
            thread.requestInterruption()

    def is_cancelled(self) -> bool:
        thread = self.thread()
        return self._cancel_event.is_set() or bool(thread and thread.isInterruptionRequested())

    def _emit_progress(self, event: object) -> None:
        self.progress.emit(event)

    @Slot()
    def run(self) -> None:
        self.started.emit()
        try:
            result = self._operation(self._emit_progress, self.is_cancelled)
        except CancelledError:
            get_logger().info("worker_cancelled")
            self.cancelled.emit()
        except Exception as error:
            mapped = friendly_error(error)
            get_logger().error(
                "worker_failed category=%s exception=%s",
                mapped.code,
                error.__class__.__name__,
            )
            self.failed.emit(mapped)
        except BaseException as error:
            mapped = friendly_error(error)
            get_logger().error(
                "worker_failed category=%s exception=%s",
                mapped.code,
                error.__class__.__name__,
            )
            self.failed.emit(mapped)
        else:
            self.succeeded.emit(result)


class _ProgressForwarder(QObject):
    """Carry one worker's progress to the main thread, owner included.

    ``JobWorker.progress`` is emitted from yt-dlp's own fragment threads rather
    than from the thread the worker lives in, and a signal connected to a plain
    callable only runs when that worker thread's event loop gets to it -- which
    is after ``JobWorker.run`` has returned, because the job occupies the loop
    for its whole duration.  Measured on a real download: an event emitted at
    9.667 s was relayed at 20.683 s, the instant the job ended, so the bar
    received the entire history of the download in one burst and nothing
    before it.  A slot on a QObject owned by the main thread is delivered
    there instead, while the job is still running (same measurement, emitted
    4.012 s and delivered 4.031 s), which is where the progress bar is.

    The forwarder belongs to a single job on purpose: it is what
    ``JobController._relay_progress`` is handed, so a late event from a
    superseded worker is still recognisable as one and can be dropped.
    """

    def __init__(self, controller: JobController, owner: JobWorker) -> None:
        super().__init__(controller)
        self._controller = controller
        self._owner = owner

    @Slot(object)
    def forward(self, event: object) -> None:
        self._controller._relay_progress(self._owner, event)


class JobController(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: JobWorker | None = None
        self._forwarder: _ProgressForwarder | None = None

    @property
    def is_running(self) -> bool:
        # Keep the controller busy until the queued QThread.finished slot has
        # run.  QThread.isRunning() can become false just before that slot.
        return self._thread is not None

    def start(self, operation: Callable[[Callable[[object], None], Callable[[], bool]], Any]) -> None:
        if self.is_running:
            raise RuntimeError("A background job is already running.")
        thread = QThread()
        worker = JobWorker(operation)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        thread.started.connect(worker.run)
        # The worker is captured by a forwarder on this controller's thread
        # rather than by a lambda here, because a plain callable would be run
        # on the worker's own thread, whose event loop the job is occupying --
        # see _ProgressForwarder.  The forwarder also carries the owner, which
        # QObject.sender() cannot: it is not exposed to Python in PySide6
        # 6.11.2 and raises NameError.
        self._forwarder = _ProgressForwarder(self, worker)
        worker.progress.connect(self._forwarder.forward)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.start()

    def _relay_progress(self, owner: JobWorker, event: object) -> None:
        """Forward a worker's progress, unless that worker has been superseded.

        The owner is passed as an argument rather than recovered with
        ``QObject.sender()``, which is not exposed to Python in PySide6
        6.11.2, and this runs on the main thread together with ``start``,
        which is what sets ``self._worker``: a superseding job is therefore
        already visible here when one has begun.  Dropping the old worker's
        event is what stops the progress bar pinning itself -- a value from a
        finished operation that arrives after the next operation reset the bar
        is not a retry reporting fewer bytes, and treating it as one clamps
        every later value to 100.
        """

        if self._worker is not None and self._worker is not owner:
            return
        self.progress.emit(event)

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def wait(self, milliseconds: int = 10_000) -> bool:
        if self._thread is None:
            return True
        return self._thread.wait(milliseconds)

    def _on_succeeded(self, result: object) -> None:
        self.succeeded.emit(result)

    def _on_failed(self, error: object) -> None:
        if isinstance(error, AppError):
            self.failed.emit(error)
        else:
            self.failed.emit(friendly_error(RuntimeError(str(error))))

    def _on_cancelled(self) -> None:
        self.cancelled.emit()

    def _on_thread_finished(self) -> None:
        if self._thread is None:
            return
        # The worker and QThread are released only after QThread.finished has
        # arrived.  Deferring worker.deleteLater() from the worker's outcome
        # signal races with queued QThread teardown in some PySide6 builds.
        self._thread = None
        self._worker = None
        forwarder = self._forwarder
        self._forwarder = None
        if forwarder is not None:
            # Every progress event was posted to this thread before the job
            # returned, and QThread.finished is posted after it, so nothing is
            # still on its way to a forwarder that is about to go.
            forwarder.deleteLater()
        self.finished.emit()
