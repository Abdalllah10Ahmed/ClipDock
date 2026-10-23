"""The download history, in a window of its own, grouped by job.

It is a separate window rather than a pane in the main one because the answer
it gives is not the answer the main window is giving.  The main window is about
the download that is happening; this is about the two hundred that already
happened, and taking the space for them would mean scrolling past an archive to
reach the Pause button.

**Grouped, not flat.**  A job is one row with its own summary - what was asked
for, the folder, when it ran, how many were saved - and the files it produced
are listed underneath it.  That is the user's decision and the correct one: a
200-video playlist is one thing a person did, and 200 unrelated lines would
bury every single-video download ever made underneath it.  The newest job is
therefore open and the older ones are folded, so "what did I just get" is
answered on sight and an old one is still found by scrolling.

**State is the first thing read.**  A coloured chip says completed, paused,
interrupted, partly saved, failed, or cancelled before any sentence is parsed,
because those are the six things a history row can mean and a person scanning
the list is asking which one.  The chip is drawn rather than painted as text so
it survives both themes; its text is still in the model, so the window can be
read the way a tree is normally read.

**Actions belong to the row that knows the answer.**  Every row offers the
folder it wrote to.  A job that left pieces on disk offers *Resume*: it re-runs
the same request and continues from those pieces, including after the program
was closed, because they live in a staging folder beside the videos rather than
in a temporary directory.  A job that failed, or stopped with nothing left to
continue, offers *Retry*.  Nothing is resumed automatically - the button is the
only way, and it says which of the two it is.

The window reads the file when it is opened or refreshed and writes only when
asked to erase.  It never raises: a history that could not be read shows as an
empty history with an honest label, because a log file is not allowed to be the
reason a window will not open.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.engine import unfinished_downloads
from ..core.history import (
    ITEM_FAILED,
    ITEM_SAVED,
    ITEM_STOPPED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_PARTIAL,
    OUTCOME_PAUSED,
    clear_history,
    read_history,
)

_LOGGER = logging.getLogger(__name__)

COLUMN_WHEN = 0
COLUMN_WHAT = 1
COLUMN_STATE = 2
COLUMN_WHERE = 3

# The state key is stored beside the display text, so the delegate can colour a
# chip without parsing the sentence and the rest of the window can decide what a
# row offers without repeating itself.
_STATE_ROLE = int(Qt.ItemDataRole.UserRole) + 1

MODE_LABELS = {
    "video": "Video",
    "audio": "Audio",
    "thumbnail": "Thumbnail",
    "subtitles": "Subtitles",
    "playlist": "Playlist",
    "queue": "Batch queue",
}

# Spelled out rather than derived from the enums' values, because these are the
# words a person reads and the enum values are the words on disk.  Keeping them
# apart is what lets the file's format stay stable while the wording is
# rephrased later.
STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_PAUSED = "paused"
STATE_INTERRUPTED = "interrupted"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"

STATE_LABELS = {
    STATE_COMPLETE: "Completed",
    STATE_PARTIAL: "Partly saved",
    STATE_PAUSED: "Paused",
    STATE_INTERRUPTED: "Interrupted",
    STATE_FAILED: "Failed",
    STATE_CANCELLED: "Cancelled",
    STATE_UNKNOWN: "Unknown",
}

STATE_FROM_OUTCOME = {
    OUTCOME_COMPLETE: STATE_COMPLETE,
    OUTCOME_PARTIAL: STATE_PARTIAL,
    OUTCOME_PAUSED: STATE_PAUSED,
    OUTCOME_INTERRUPTED: STATE_INTERRUPTED,
    OUTCOME_FAILED: STATE_FAILED,
    OUTCOME_CANCELLED: STATE_CANCELLED,
}

# White text on a saturated fill, so the same chip is legible in both themes.
STATE_COLORS = {
    STATE_COMPLETE: "#2e7d32",
    STATE_PARTIAL: "#8a5a00",
    STATE_PAUSED: "#1565c0",
    STATE_INTERRUPTED: "#b26a00",
    STATE_FAILED: "#b3261e",
    STATE_CANCELLED: "#5f6368",
    STATE_UNKNOWN: "#5f6368",
}

# Outcomes whose pieces are worth offering to continue.  A cancellation is
# included because the app records an app-close as interrupted but an older
# version recorded it as cancelled; the staging folder is the real evidence.
_RESUMABLE_OUTCOMES = frozenset(
    {OUTCOME_PAUSED, OUTCOME_INTERRUPTED, OUTCOME_CANCELLED}
)
_RERUNNABLE_OUTCOMES = frozenset(
    {
        OUTCOME_PAUSED,
        OUTCOME_INTERRUPTED,
        OUTCOME_CANCELLED,
        OUTCOME_FAILED,
        OUTCOME_PARTIAL,
    }
)

ITEM_LABELS = {
    ITEM_SAVED: "Saved",
    ITEM_FAILED: "Failed",
    ITEM_STOPPED: "Stopped",
}


def _when(value: Any) -> str:
    """A timestamp as it is written, with the date and the clock separated.

    Not parsed.  Parsing would need a locale, and a value that could not be
    parsed would have to be handled anyway - so the only honest formatting is
    the one that cannot fail on a record somebody else wrote.
    """

    return str(value or "").replace("T", " ")


def _count_line(job: dict[str, Any]) -> str:
    """How much of the job arrived, as a phrase that fits beside its state.

    Empty when there is nothing worth counting: a stopped job already says so
    in its outcome, and counting its untouched files as failures would be the
    log claiming something it never watched.
    """

    if job.get("outcome") in (
        OUTCOME_PAUSED,
        OUTCOME_CANCELLED,
        OUTCOME_INTERRUPTED,
    ):
        return ""
    succeeded = int(job.get("succeeded") or 0)
    failed = int(job.get("failed") or 0)
    total = max(int(job.get("total") or 0), succeeded + failed)
    if not total:
        return ""
    if failed:
        return f"{succeeded} of {total} saved"
    if succeeded <= 1:
        return "1 file saved" if succeeded else "nothing was saved"
    return f"{succeeded} files saved"


def _outcome_line(job: dict[str, Any]) -> str:
    outcome = str(job.get("outcome") or "")
    state = STATE_FROM_OUTCOME.get(outcome, STATE_UNKNOWN)
    count = _count_line(job)
    line = f"{STATE_LABELS[state]} · {count}" if count else STATE_LABELS[state]
    reason = str(job.get("reason") or "")
    if reason and state != STATE_COMPLETE:
        line = f"{line} - {reason}"
    return line


def _item_outcome_line(item: dict[str, Any]) -> str:
    outcome = str(item.get("outcome") or "")
    label = ITEM_LABELS.get(outcome, outcome.capitalize() if outcome else "Unknown")
    reason = str(item.get("reason") or "")
    return f"{label} - {reason}" if reason else label


def _job_what(job: dict[str, Any]) -> str:
    """What the job was: its kind and the link it was given."""

    mode = str(job.get("mode") or "")
    kind = MODE_LABELS.get(mode, mode.capitalize() if mode else "Download")
    asked = str(job.get("requested") or "")
    return f"{kind} · {asked}" if asked else kind


def _https(url: Any) -> str:
    """A link this window is willing to open, or nothing.

    The same rule the update notice follows: a link offered on the user's
    behalf is opened over https or not at all, because "the page this file came
    from" is exactly the sentence a redirect would love to fill in.
    """

    text = str(url or "")
    return text if text.lower().startswith("https://") else ""


def _job_request(job: dict[str, Any]) -> dict[str, Any]:
    request = job.get("request")
    return request if isinstance(request, dict) else {}


def _job_folder(job: dict[str, Any]) -> str:
    folder = str(job.get("folder") or "")
    if not folder:
        folder = str(_job_request(job).get("output_dir") or "")
    return folder


def _has_pieces(job: dict[str, Any]) -> bool:
    """Whether this job left something on disk that a continuation can use.

    A paused or interrupted job is only offered Resume when there are real
    pieces, because a Resume that starts from nothing is a Retry wearing the
    wrong word.  The scan is the same one the main window uses; it reads the
    hidden staging folder as well as the destination, so a job stopped in an
    older version is still found.
    """

    if str(job.get("outcome") or "") not in _RESUMABLE_OUTCOMES:
        return False
    if not _job_request(job) and not job.get("requested"):
        return False
    folder = _job_folder(job)
    if not folder:
        return False
    try:
        pieces = unfinished_downloads(Path(folder))
    except OSError:
        return False
    # A `.ytdl` file is resume bookkeeping, not downloaded bytes.  Offering
    # Resume over one would be offering to continue nothing.
    return any(item.has_fragments for item in pieces)


def _can_resume(job: dict[str, Any]) -> bool:
    request = _job_request(job)
    if not request.get("url") and not request.get("urls"):
        return False
    return _has_pieces(job)


def _can_retry(job: dict[str, Any]) -> bool:
    outcome = str(job.get("outcome") or "")
    if outcome not in _RERUNNABLE_OUTCOMES:
        return False
    request = _job_request(job)
    return bool(request.get("url") or request.get("urls"))


class _StateChipDelegate(QStyledItemDelegate):
    """Draw the state as a chip, and the count beside it as ordinary text."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: Any,
    ) -> None:
        state = str(index.data(_STATE_ROLE) or "")
        if not state:
            super().paint(painter, option, index)
            return
        # The row background (selection, alternating colour, focus) is still the
        # view's job; the chip is painted on top of it.
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem,
            option,
            painter,
            option.widget,
        )
        label = STATE_LABELS.get(state, STATE_LABELS[STATE_UNKNOWN])
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or label)
        detail = text[len(label):].lstrip(" ·-") if text.startswith(label) else ""

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        metrics = option.fontMetrics
        chip_width = metrics.horizontalAdvance(label) + 18
        height = min(20, max(16, option.rect.height() - 6))
        chip = QRect(
            option.rect.left() + 6,
            option.rect.top() + (option.rect.height() - height) // 2,
            min(chip_width, max(0, option.rect.width() - 14)),
            height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(STATE_COLORS.get(state, STATE_COLORS[STATE_UNKNOWN])))
        painter.drawRoundedRect(chip, height / 2, height / 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, label)

        if detail:
            available = option.rect.adjusted(chip.right() + 8, 0, -6, 0)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            colour = option.palette.color(
                QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
            )
            painter.setPen(colour)
            elided = metrics.elidedText(
                detail, Qt.TextElideMode.ElideRight, max(0, available.width())
            )
            painter.drawText(
                available,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided,
            )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), 26))


class HistoryWindow(QDialog):
    """Shows every recorded job, newest first, with its files beneath it."""

    # A request to run a job again, carrying the recorded job so the main window
    # does not have to re-read the file and find it.  Nothing runs without the
    # person pressing the button that emits this.
    resume_requested = Signal(object)
    retry_requested = Signal(object)

    def __init__(self, parent: Any = None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download history")
        self.setObjectName("historyWindow")
        self.setMinimumSize(860, 480)
        self._path = path
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        heading = QLabel("Download history")
        heading.setObjectName("historyHeading")
        head.addWidget(heading)
        head.addStretch(1)
        self.clear_button = QPushButton("Clear history")
        self.clear_button.setObjectName("HistoryClear")
        self.clear_button.setToolTip("Erase every recorded job from this computer")
        self.clear_button.clicked.connect(self._clear)
        head.addWidget(self.clear_button)
        layout.addLayout(head)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("historySummary")
        layout.addWidget(self.summary_label)

        self.tree = QTreeWidget()
        self.tree.setObjectName("HistoryTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["When", "What", "State", "Where"])
        self.tree.setRootIsDecorated(True)
        # Rows are single-line text throughout, so Qt may assume they all share
        # a height.  Against a playlist that is 200 rows deep and a history that
        # can hold 200 of those, that assumption is the difference between a
        # window that opens and a window that measures every one of them first.
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._refresh_actions)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self.open_folder())
        self.tree.setItemDelegateForColumn(COLUMN_STATE, _StateChipDelegate(self.tree))
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(COLUMN_WHEN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMN_WHAT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COLUMN_STATE, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(COLUMN_STATE, 250)
        layout.addWidget(self.tree, 1)

        self.empty_label = QLabel("Nothing has been recorded yet.")
        self.empty_label.setObjectName("historyEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.folder_button = QPushButton("Open folder")
        self.folder_button.setObjectName("HistoryOpenFolder")
        self.folder_button.setToolTip("Open the folder this row's file went into")
        self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(self.open_folder)
        self.resume_button = QPushButton("Resume")
        self.resume_button.setObjectName("HistoryResume")
        self.resume_button.setToolTip(
            "Continue this download from the pieces it kept on disk"
        )
        self.resume_button.setVisible(False)
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self._resume_selected)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("HistoryRetry")
        self.retry_button.setToolTip("Run this download again from the beginning")
        self.retry_button.setVisible(False)
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._retry_selected)
        self.link_button = QPushButton("Open source page")
        self.link_button.setObjectName("HistoryOpenLink")
        self.link_button.setToolTip("Open the page this row came from in the browser")
        self.link_button.setEnabled(False)
        self.link_button.clicked.connect(self.open_link)
        foot.addWidget(self.folder_button)
        foot.addWidget(self.resume_button)
        foot.addWidget(self.retry_button)
        foot.addWidget(self.link_button)
        foot.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        foot.addWidget(close_button)
        layout.addLayout(foot)

    # ----------------------------------------------------------------- filling

    def refresh(self) -> None:
        """Re-read the file and rebuild the list.

        Newest first, newest open.  Only the newest is expanded because that is
        the one with a question attached to it; a history that opened all two
        hundred jobs would answer none of them and take a second to draw.
        """

        try:
            jobs = read_history(self._path)
        except Exception as error:  # pragma: no cover - read_history does not raise
            _LOGGER.error("history_refresh_failed exception=%s", type(error).__name__)
            jobs = []

        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            for position, job in enumerate(reversed(jobs)):
                top = self._add_job(job)
                if position == 0:
                    top.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)

        empty = not jobs
        self.tree.setVisible(not empty)
        self.empty_label.setVisible(empty)
        self.clear_button.setEnabled(not empty)
        if empty:
            self.summary_label.setText("")
        else:
            self.summary_label.setText(self._summary(jobs))
        self.tree.clearSelection()
        self._refresh_actions()

    @staticmethod
    def _summary(jobs: list[dict[str, Any]]) -> str:
        """A count of jobs and the states worth knowing are in them.

        A list of totals per state, in the order the chips use, and only states
        that occur: "12 jobs · 8 completed · 2 paused · 1 failed" answers the
        question the window is opened with far better than a bare count.
        """

        counts: dict[str, int] = {}
        for job in jobs:
            state = STATE_FROM_OUTCOME.get(str(job.get("outcome") or ""), STATE_UNKNOWN)
            counts[state] = counts.get(state, 0) + 1
        pieces = [f"{len(jobs)} {'job' if len(jobs) == 1 else 'jobs'}"]
        for state in (
            STATE_COMPLETE,
            STATE_PARTIAL,
            STATE_PAUSED,
            STATE_INTERRUPTED,
            STATE_FAILED,
            STATE_CANCELLED,
            STATE_UNKNOWN,
        ):
            count = counts.get(state)
            if count:
                pieces.append(f"{count} {STATE_LABELS[state].lower()}")
        return " · ".join(pieces) + " · newest first"

    def _add_job(self, job: dict[str, Any]) -> QTreeWidgetItem:
        folder = _job_folder(job)
        asked = str(job.get("requested") or "")
        # Only the link for the job itself, and only if it really is one: a
        # batch job records "3 links" here rather than a URL, and there is no
        # single page behind it.
        url = _https(asked) if asked.lower().startswith(("http://", "https://")) else ""
        folder_exists = bool(folder) and Path(folder).is_dir()
        state = STATE_FROM_OUTCOME.get(str(job.get("outcome") or ""), STATE_UNKNOWN)

        top = QTreeWidgetItem(self.tree)
        top.setText(COLUMN_WHEN, _when(job.get("started")))
        top.setText(COLUMN_WHAT, _job_what(job))
        top.setText(COLUMN_STATE, _outcome_line(job))
        top.setText(COLUMN_WHERE, folder)
        top.setToolTip(COLUMN_WHERE, folder)
        top.setData(
            COLUMN_WHAT,
            Qt.ItemDataRole.UserRole,
            {
                "folder": folder if folder_exists else "",
                "url": _https(url),
                "job": job,
                "state": state,
                "resumable": _can_resume(job),
                "retryable": _can_retry(job),
                "destination": "",
            },
        )
        top.setData(COLUMN_STATE, _STATE_ROLE, state)
        bold = QFont(top.font(COLUMN_WHAT))
        bold.setBold(True)
        top.setFont(COLUMN_WHAT, bold)

        for entry in job.get("items") or ():
            title = str(entry.get("title") or entry.get("destination") or "")
            destination = str(entry.get("destination") or "")
            child = QTreeWidgetItem(top)
            child.setText(COLUMN_WHEN, "")
            child.setText(COLUMN_WHAT, title)
            child.setText(COLUMN_STATE, _item_outcome_line(entry))
            child.setText(COLUMN_WHERE, destination)
            child.setToolTip(COLUMN_WHAT, title)
            child.setToolTip(COLUMN_WHERE, destination)
            child.setData(
                COLUMN_WHAT,
                Qt.ItemDataRole.UserRole,
                {
                    # The job's folder rather than the file's parent: a failed
                    # row has no file, and every row of one job wrote into one
                    # place.  The destination is kept so a child can open the
                    # exact folder its own file went to.
                    "folder": folder if folder_exists else "",
                    "url": _https(entry.get("url")),
                    "job": job,
                    "state": state,
                    "resumable": _can_resume(job),
                    "retryable": _can_retry(job),
                    "destination": destination,
                },
            )
        return top

    # ----------------------------------------------------------------- actions

    def _selected(self) -> dict[str, Any]:
        item = self.tree.currentItem()
        if item is None:
            return {}
        data = item.data(COLUMN_WHAT, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, dict) else {}

    def _action_folder(self, data: dict[str, Any]) -> str:
        """The folder a row's Open folder should show.

        For a file row that is the file's own parent, so a history with mixed
        destinations can still open the right one.  For a job row it is the
        folder the job wrote into.
        """

        destination = str(data.get("destination") or "")
        if destination:
            parent = Path(destination).parent
            if parent.is_dir():
                return str(parent)
        folder = str(data.get("folder") or "")
        return folder if folder and Path(folder).is_dir() else ""

    def _refresh_actions(self) -> None:
        data = self._selected()
        folder = self._action_folder(data)
        self.folder_button.setEnabled(bool(folder))
        self.link_button.setEnabled(bool(_https(data.get("url"))))
        resumable = bool(data.get("resumable"))
        retryable = bool(data.get("retryable")) and not resumable
        self.resume_button.setVisible(resumable)
        self.resume_button.setEnabled(resumable)
        self.retry_button.setVisible(retryable)
        self.retry_button.setEnabled(retryable)

    def open_folder(self) -> None:
        """Open the folder behind the selected row, and say nothing if it is gone."""

        folder = self._action_folder(self._selected())
        if not folder:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def open_link(self) -> None:
        url = _https(self._selected().get("url"))
        if not url:
            return
        QDesktopServices.openUrl(QUrl(url))

    def _resume_selected(self) -> None:
        data = self._selected()
        job = data.get("job")
        if not data.get("resumable") or not isinstance(job, dict):
            return
        self.resume_requested.emit(job)

    def _retry_selected(self) -> None:
        data = self._selected()
        job = data.get("job")
        # A row with pieces offers Resume rather than Retry, because continuing
        # is the truer action; the button hiding is not enough on its own since
        # the same guard is reached by the context menu.
        if not data.get("retryable") or data.get("resumable") or not isinstance(job, dict):
            return
        self.retry_requested.emit(job)

    def _context_menu(self, position: Any) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        self._refresh_actions()

        menu = QMenu(self)
        folder_action = menu.addAction("Open folder")
        folder_action.setEnabled(self.folder_button.isEnabled())
        link_action = menu.addAction("Open source page")
        link_action.setEnabled(self.link_button.isEnabled())
        menu.addSeparator()
        resume_action = menu.addAction("Resume")
        resume_action.setVisible(self.resume_button.isVisible())
        retry_action = menu.addAction("Retry")
        retry_action.setVisible(self.retry_button.isVisible())
        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is folder_action:
            self.open_folder()
        elif chosen is link_action:
            self.open_link()
        elif chosen is resume_action:
            self._resume_selected()
        elif chosen is retry_action:
            self._retry_selected()

    def _clear(self) -> None:
        """Erase the record, after asking.

        Asked for because it cannot be undone: the downloaded files stay where
        they are, but the answer to "what did I get last week" is the one thing
        this window exists to hold, and losing it to a stray click is not a
        trade anybody agreed to.
        """

        answer = QMessageBox.question(
            self,
            "Clear history",
            "Erase every recorded job from this computer?\n\n"
            "The downloaded files themselves are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            erased = clear_history(self._path)
        except Exception as error:  # pragma: no cover - clear_history does not raise
            _LOGGER.error("history_clear_failed exception=%s", type(error).__name__)
            erased = False
        if not erased:
            QMessageBox.warning(
                self,
                "Could not clear history",
                "The history file could not be rewritten, so it was left as it was.",
            )
            return
        self.refresh()
