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

**Two things are offered from every row**, because both are things the row is
the only place that knows them: the folder the file went into, and the page it
came from.  The second is opened only over `https` - a `html_url` handed
straight to the desktop is a link on the user's behalf, and this program does
not make those.

The window reads the file when it is opened or refreshed and writes only when
asked to erase.  It never raises: a history that could not be read shows as an
empty history with an honest label, because a log file is not allowed to be the
reason a window will not open.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.history import (
    ITEM_FAILED,
    ITEM_SAVED,
    ITEM_STOPPED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_PAUSED,
    clear_history,
    read_history,
)

_LOGGER = logging.getLogger(__name__)

COLUMN_WHEN = 0
COLUMN_WHAT = 1
COLUMN_OUTCOME = 2
COLUMN_WHERE = 3

# Spelled out rather than derived from the enums' values, because these are the
# words a person reads and the enum values are the words on disk.  Keeping them
# apart is what lets the file's format stay stable while the wording is
# rephrased later.
MODE_LABELS = {
    "video": "Video",
    "audio": "Audio",
    "thumbnail": "Thumbnail",
    "subtitles": "Subtitles",
    "playlist": "Playlist",
    "queue": "Batch queue",
}
OUTCOME_LABELS = {
    OUTCOME_COMPLETE: "Complete",
    OUTCOME_PARTIAL: "Partly saved",
    OUTCOME_FAILED: "Failed",
    OUTCOME_PAUSED: "Paused",
    OUTCOME_CANCELLED: "Cancelled",
}
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
    """How much of the job arrived, as a phrase that fits beside its outcome.

    Empty when there is nothing worth counting: a stopped job already says so
    in its outcome, and counting its untouched files as failures would be the
    log claiming something it never watched.
    """

    if job.get("outcome") in (OUTCOME_PAUSED, OUTCOME_CANCELLED):
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
    label = OUTCOME_LABELS.get(outcome, outcome.capitalize() if outcome else "Unknown")
    count = _count_line(job)
    line = f"{label} · {count}" if count else label
    reason = str(job.get("reason") or "")
    if reason and outcome != OUTCOME_COMPLETE:
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


class HistoryWindow(QDialog):
    """Shows every recorded job, newest first, with its files beneath it."""

    def __init__(self, parent: Any = None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download history")
        self.setObjectName("historyWindow")
        self.setMinimumSize(820, 440)
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
        self.tree.setHeaderLabels(["When", "What", "Outcome", "Where"])
        self.tree.setAlternatingRowColors(True)
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
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(COLUMN_WHEN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COLUMN_WHAT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COLUMN_OUTCOME, QHeaderView.ResizeMode.ResizeToContents)
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
        self.link_button = QPushButton("Open source page")
        self.link_button.setObjectName("HistoryOpenLink")
        self.link_button.setToolTip("Open the page this row came from in the browser")
        self.link_button.setEnabled(False)
        self.link_button.clicked.connect(self.open_link)
        foot.addWidget(self.folder_button)
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
            count = len(jobs)
            noun = "job" if count == 1 else "jobs"
            self.summary_label.setText(f"{count} {noun} recorded · newest first")
        self.tree.clearSelection()
        self._refresh_actions()

    def _add_job(self, job: dict[str, Any]) -> QTreeWidgetItem:
        folder = str(job.get("folder") or "")
        asked = str(job.get("requested") or "")
        # Only the link for the job itself, and only if it really is one: a
        # batch job records "3 links" here rather than a URL, and there is no
        # single page behind it.
        url = _https(asked) if asked.lower().startswith(("http://", "https://")) else ""
        folder_exists = bool(folder) and Path(folder).is_dir()

        top = QTreeWidgetItem(self.tree)
        top.setText(COLUMN_WHEN, _when(job.get("started")))
        top.setText(COLUMN_WHAT, _job_what(job))
        top.setText(COLUMN_OUTCOME, _outcome_line(job))
        top.setText(COLUMN_WHERE, folder)
        top.setToolTip(COLUMN_WHERE, folder)
        top.setData(
            COLUMN_WHAT,
            Qt.ItemDataRole.UserRole,
            {"folder": folder if folder_exists else "", "url": _https(url)},
        )
        bold = QFont(top.font(COLUMN_WHAT))
        bold.setBold(True)
        top.setFont(COLUMN_WHAT, bold)

        for entry in job.get("items") or ():
            child = QTreeWidgetItem(top)
            title = str(entry.get("title") or entry.get("destination") or "")
            destination = str(entry.get("destination") or "")
            child.setText(COLUMN_WHEN, "")
            child.setText(COLUMN_WHAT, title)
            child.setText(COLUMN_OUTCOME, _item_outcome_line(entry))
            child.setText(COLUMN_WHERE, destination)
            child.setToolTip(COLUMN_WHAT, title)
            child.setToolTip(COLUMN_WHERE, destination)
            child.setData(
                COLUMN_WHAT,
                Qt.ItemDataRole.UserRole,
                # The job's folder rather than the file's parent: a failed row
                # has no file, and every row of one job wrote into one place.
                {"folder": folder if folder_exists else "", "url": _https(entry.get("url"))},
            )
        return top

    # ----------------------------------------------------------------- actions

    def _selected(self) -> dict[str, Any]:
        item = self.tree.currentItem()
        if item is None:
            return {}
        data = item.data(COLUMN_WHAT, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, dict) else {}

    def _refresh_actions(self) -> None:
        data = self._selected()
        folder = str(data.get("folder") or "")
        self.folder_button.setEnabled(bool(folder) and Path(folder).is_dir())
        self.link_button.setEnabled(bool(_https(data.get("url"))))

    def open_folder(self) -> None:
        """Open the folder behind the selected row, and say nothing if it is gone."""

        folder = str(self._selected().get("folder") or "")
        if not folder or not Path(folder).is_dir():
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def open_link(self) -> None:
        url = _https(self._selected().get("url"))
        if not url:
            return
        QDesktopServices.openUrl(QUrl(url))

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
        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is folder_action:
            self.open_folder()
        elif chosen is link_action:
            self.open_link()

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
