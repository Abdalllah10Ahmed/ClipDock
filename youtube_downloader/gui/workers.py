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
        worker.progress.connect(self.progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.start()

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
        self.finished.emit()
