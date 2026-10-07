from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QCheckBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.binaries import find_ffmpeg
from ..core.dependencies import (
    FFMPEG_INSTALLED_BYTES,
    clear_cached_ffmpeg,
    forget_discovery,
    installed_ffmpeg,
    missing_dependencies,
    ytdlp_version,
)
from ..core.engine import (
    Engine,
    UnfinishedDownload,
    playlist_audio_size_bytes,
    playlist_video_size_bytes,
    quality_matches_target,
    select_playlist_video_quality,
    unfinished_downloads,
)
from ..core.errors import AppError
from ..core.logging_setup import get_logger
from ..core.models import (
    MAX_SUBTITLE_LANGUAGES,
    DownloadMode,
    DownloadRequest,
    PlaylistDownloadRequest,
    PlaylistDownloadResult,
    PlaylistInfo,
    PlaylistMedia,
    PlaylistQuality,
    QueueDownloadRequest,
    QueueDownloadResult,
    StreamPreference,
    SubtitleFormat,
    SubtitleSource,
    VideoInfo,
    VideoQuality,
    estimate_audio_size_bytes,
    format_size,
)
from ..core.settings import read_settings, write_setting
from ..core.updates import RELEASES_URL, UpdateCheck, check_for_updates
from ..core.urls import UrlValidationError, normalize_youtube_playlist_url, normalize_youtube_url
from . import caption
from .caption import CaptionButton
from .dependencies import DependencyDialog, checked_devices
from .selectors import SelectorComboBox
from .themes import DEFAULT_THEME, THEMES, THEME_IDS, is_dark, theme_palette
from .workers import JobController

# The product's name.  Everything user-visible reads from here - the window
# title, the drawn title bar, the taskbar entry, and the About box - so the
# three can never drift apart.
APP_NAME = "ClipDock"

# The height of the drawn caption strip. The three window buttons are built to
# this height so their hover highlight reaches the top and bottom edges of the
# bar instead of floating in the middle of it, which is what makes them read as
# part of the frame rather than as buttons placed on top of one.
TITLE_BAR_HEIGHT = 40

# The settings key the chosen theme is stored under.
THEME_SETTING_KEY = "theme"
# Whether to embed cover art in MP3 downloads.  Stored as "1" or "0".
EMBED_COVER_SETTING_KEY = "embed_cover"
# Whether ClipDock may ask GitHub about a newer release on its own, and the
# date it last did.  Both are stored as plain strings like everything else
# here: "1"/"0" for the switch, and an ISO date so "has it run today" needs no
# clock plumbing and survives a machine whose clock was wrong for a moment.
#
# The switch exists because the check is now automatic, and an unasked-for
# network request has to be possible to refuse.  The date exists so that a
# single launch cannot ask repeatedly: one request a day at most, whatever
# happens.
UPDATE_CHECK_SETTING_KEY = "update_check"
UPDATE_CHECK_DATE_KEY = "update_check_date"

# How long after the window appears before the automatic check runs.  Long
# enough that startup is genuinely over and the person has had the window for
# a moment; short enough that a notice still arrives while they are looking at
# it.  It is deliberately not zero: the check is a network request competing
# with nothing in particular, and doing it during startup would make launch
# slower for everyone to benefit the few who have an update waiting.
UPDATE_CHECK_DELAY_MS = 8000

_LOGGER = logging.getLogger(__name__)

# WM_NCHITTEST and the hit-test codes it expects back.  Qt does not export
# these, and answering it wrong is how a frameless window ends up unresizable.
_WM_NCHITTEST = 0x0084
_HT_LEFT = 10
_HT_RIGHT = 11
_HT_TOP = 12
_HT_TOPLEFT = 13
_HT_TOPRIGHT = 14
_HT_BOTTOM = 15
_HT_BOTTOMLEFT = 16
_HT_BOTTOMRIGHT = 17

# GetSystemMetrics indices for the frame a native window would have.
_SM_CXSIZEFRAME = 11
_SM_CXPADDEDBORDER = 92

# Upper bound on the resize grip, in pixels.  See MainWindow._resize_border.
_MAX_RESIZE_BORDER = 10

# DWM window attributes used for the rounded corners and the 1px window border.
# Neither is exported by any Python package, so the numbers are stated here.
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34


def _windows_resize_border() -> int:
    """The frame thickness Windows itself would use, in pixels.

    Asking the style for a pixel metric is not an option here: PySide6 does not
    expose every metric the C++ enum has, and the two that would fit
    (``PM_ComboBoxArrowWidth`` and ``PM_MouseTitleBarHeight``) are among the ones
    it does not.  ``GetSystemMetrics`` reports the value the shell itself uses
    to size its borders, so it also scales with the display.
    """

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        # SM_CXSIZEFRAME and SM_CXPADDEDBORDER together are the invisible
        # resize margin a native window has on this display.
        return int(
            user32.GetSystemMetrics(_SM_CXSIZEFRAME)
            + user32.GetSystemMetrics(_SM_CXPADDEDBORDER)
        )
    except (AttributeError, OSError, ValueError):  # pragma: no cover - platform guard
        return 0

PLAYLIST_COLUMN_CHECK = 0
PLAYLIST_COLUMN_TITLE = 1
PLAYLIST_COLUMN_LENGTH = 2
PLAYLIST_COLUMN_SIZE = 3
PLAYLIST_COLUMN_COUNT = 4

QUEUE_COLUMN_STATUS = 0
QUEUE_COLUMN_LINK = 1
QUEUE_COLUMN_TITLE = 2
QUEUE_COLUMN_SIZE = 3
QUEUE_COLUMN_COUNT = 4

# The queue uses a fixed set of common resolutions rather than a probed list,
# because a batch of links can come from unrelated channels and is not read
# before the job starts.  Each link resolves to the nearest available height.
QUEUE_HEIGHT_CHOICES = (2160, 1440, 1080, 720, 480, 360)

# A queue entry is a single link, so these are the per-link media kinds.
# Playlist and queue are jobs in their own right and never nest.
_QUEUE_MODES = (
    DownloadMode.VIDEO,
    DownloadMode.AUDIO,
    DownloadMode.THUMBNAIL,
    DownloadMode.SUBTITLES,
)

QUEUE_MEDIA_LABELS = {
    DownloadMode.VIDEO: "Video (MP4)",
    DownloadMode.AUDIO: "Audio (MP3)",
    DownloadMode.THUMBNAIL: "Thumbnail",
    DownloadMode.SUBTITLES: "Subtitles",
}

# Files that are not worth handing to a media player, so the Play button is
# disabled when the last job only produced these.
SUBTITLE_EXTENSIONS = frozenset(item.extension for item in SubtitleFormat)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _piece_fingerprint(
    item: UnfinishedDownload,
) -> tuple[tuple[str, ...], int | None]:
    """What an interrupted download's pieces looked like at one moment.

    Compared before and after a job to decide which leftovers in the folder that
    job is responsible for.  Counting every unfinished file in the destination
    instead would count an earlier session's crash as part of this one, and a
    tooltip that offers to continue two downloads when it will continue one is
    the same wrong count the Retry button was rebuilt to avoid -- it has simply
    moved to a different button.

    The piece *names* are part of the answer and not only their sizes: a run
    that opened a `.part` left behind by an earlier session writes to the same
    name, so its fingerprint changes and it is correctly claimed, while a
    fragment that came and went during the job is claimed the same way.
    """

    return (tuple(part.name for part in item.parts), item.total_bytes)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine | None = None) -> None:
        super().__init__()
        self.engine = engine or Engine()
        self.controller = JobController(self)
        self.info: VideoInfo | None = None
        self.playlist_info: PlaylistInfo | None = None
        self._progress_value = -1
        self._last_directory = self._default_download_directory()
        self._playlist_selection: dict[str, bool] = {}
        self._updating_playlist_rows = False
        # Paths from the most recent successful job, used by Open folder / Play.
        self._last_result_paths: tuple[Path, ...] = ()
        self._last_result_folder: Path | None = None
        # What the last playlist or batch job failed on, so Retry can re-run just
        # those: video ids for a playlist, whole links for a batch.  Each list is
        # kept for its own mode, so switching between them does not lose either.
        self._last_failed_playlist: tuple[str, ...] = ()
        self._last_failed_queue: tuple[str, ...] = ()
        # Pause and Resume, and the three facts each one needs.
        #
        # `_active_request` and `_active_operation` are the only record of *how*
        # to continue a download: a `.part` file carries no link, and the
        # settings file remembers UI preferences, never a job, so nothing
        # reconstructs a request once the program is closed.  `_resume_snapshot`
        # is what makes Resume's count honest -- a folder can hold leftovers from
        # an earlier session, and offering to continue those as though they were
        # the job just stopped would be the wrong count in a new place.
        self._pauseable = False
        self._pausing = False
        self._active_request: Any = None
        self._active_operation: Any = None
        self._paused_request: Any = None
        self._resume_snapshot: dict[str, tuple[tuple[str, ...], int | None]] = {}
        # The automatic update check, and the one piece of state it needs.
        #
        # Both the manual Help-menu check and this one produce an UpdateCheck
        # that lands in the same handler, so `_update_check_automatic` records
        # which one it was before the job starts.  It is a flag rather than an
        # argument because the handler is reached from a dispatcher that does
        # not know anything about update checks; the flag is set and consumed
        # by the two callers that do, and cleared by cancellation so a stopped
        # check cannot leave the next manual one reporting as though it had
        # been asked for in the background.
        self._update_check_automatic = False
        self._update_toast: QFrame | None = None
        self._update_toast_url: str = RELEASES_URL
        self._update_check_timer = QTimer(self)
        self._update_check_timer.setSingleShot(True)
        self._update_check_timer.setInterval(UPDATE_CHECK_DELAY_MS)
        self._update_check_timer.timeout.connect(self._run_scheduled_update_check)
        # The remembered choice is read before the UI is built, because the
        # theme is applied while the widgets are being created and re-applying
        # it afterwards would repaint the window for no reason.
        self._theme = self._remembered_theme()
        self._theme_is_dark = False
        self._build_ui()
        self._connect_signals()
        self._mode_changed()

    @staticmethod
    def _default_download_directory() -> Path:
        downloads = Path.home() / "Downloads"
        return downloads if downloads.exists() else Path.home()

    @staticmethod
    def _remembered_theme() -> str:
        """The theme the user last chose, so every launch looks the same.

        A stored id that no longer exists - a theme removed in an update, or a
        hand-edited file - falls back to the default instead of failing.  The
        settings read is already fail-soft, so this only has to validate.
        """

        stored = read_settings().get(THEME_SETTING_KEY)
        if stored in THEME_IDS:
            return str(stored)
        if stored is not None:
            _LOGGER.info("theme_fallback reason=unknown_saved_theme")
        return DEFAULT_THEME

    @staticmethod
    def _remembered_embed_cover() -> bool:
        """Whether the user wants cover art embedded in MP3 downloads.

        Defaults to True (the feature is on by default).  The setting is stored
        as a string "1" or "0" for simplicity and compatibility with the
        existing settings format.
        """

        stored = read_settings().get(EMBED_COVER_SETTING_KEY)
        if stored is None:
            return True
        return stored == "1"

    @staticmethod
    def _update_check_enabled() -> bool:
        """Whether ClipDock may ask GitHub about a newer release on its own.

        On unless the person turned it off, because the check now makes a
        network request nobody asked for and the only honest answer to that is
        a switch.  An absent key is every launch before the first one.  Any
        other value than ``"1"`` is a "no": the settings file is hand-editable,
        and a value this code does not recognise must not be read as
        permission to contact GitHub.
        """

        return read_settings().get(UPDATE_CHECK_SETTING_KEY, "1") == "1"

    @staticmethod
    def _set_update_check_enabled(enabled: bool) -> None:
        write_setting(UPDATE_CHECK_SETTING_KEY, "1" if enabled else "0")

    @staticmethod
    def _update_check_ran_today(today: str | None = None) -> bool:
        """Whether the automatic check has already been attempted today.

        Recorded when the check *starts* rather than when it succeeds, so the
        request is bounded to one a day whatever the outcome: an offline
        machine is not asked to reach GitHub again and again on every launch
        for a question it could not answer.  The date is passed in by the
        tests rather than read here, so "today" is a fact the test states
        instead of one it has to wait for.
        """

        target = date.today().isoformat() if today is None else today
        return read_settings().get(UPDATE_CHECK_DATE_KEY) == target

    @staticmethod
    def _remember_update_check_started() -> None:
        write_setting(UPDATE_CHECK_DATE_KEY, date.today().isoformat())

    def _build_title_bar(self) -> QWidget:
        """The replacement for the dropped native frame.

        Dragging uses ``startSystemMove()`` rather than ``move()`` so Windows
        still gets to handle the drag: that is what keeps snap layouts, the
        taskbar preview, and the aero-snap threshold working.  A frameless
        window is invisible to the shell otherwise, and users notice.
        """

        bar = QFrame(self)
        bar.setObjectName("titleBar")
        bar.setFixedHeight(TITLE_BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 0, 0)
        layout.setSpacing(0)

        # Not named "caption": that is the module holding the glyph names, and a local
        # of the same name silently shadowed it, so the three buttons were built
        # from a QLabel's attributes.
        caption_label = QLabel(APP_NAME, bar)
        caption_label.setObjectName("titleBarCaption")
        layout.addWidget(caption_label, 1)
        # Kept as an attribute: the drawn caption and the real window title have
        # to agree, and a test checks that they do.
        self.title_bar_caption = caption_label

        # An overflow menu rather than a native menu bar.  The window is frameless
        # with a drawn caption, so a menu bar above it would read as a second,
        # competing frame.  It sits in the caption strip instead, and carries the
        # only way back to the FFmpeg offer after it has been declined.
        #
        # A CaptionButton, so its mark is painted like the three beside it rather
        # than being a character in the button's font: a "?" came out at three
        # different weights against its neighbours, and a character the font
        # lacks draws as a box.
        self.help_button = CaptionButton(caption.OVERFLOW, "Help", bar)
        self.help_button.setObjectName("helpButton")
        self.help_button.setFixedSize(46, TITLE_BAR_HEIGHT)
        self.help_button.setMenu(self._build_help_menu())
        layout.addWidget(self.help_button, 0)

        self.minimize_button = self._window_button(bar, caption.MINIMIZE, "Minimize")
        self.maximize_button = self._window_button(bar, caption.MAXIMIZE, "Maximize")
        self.close_button = self._window_button(bar, caption.CLOSE, "Close")
        self.maximize_button.setObjectName("windowButtonMaximize")
        self.close_button.setObjectName("windowButtonClose")
        # Zero spacing and a zero right margin: the three buttons sit against
        # the top-right corner with no gutter, which is where the shell puts its
        # own and is the strongest single cue that this is a window frame.
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            layout.addWidget(button, 0)

        self.minimize_button.clicked.connect(self.showMinimized)
        self.maximize_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(self.close)
        # The caption and the bar itself are draggable; the buttons are not,
        # which is why the handler is installed on the bar and the caption
        # rather than on the whole window.
        for target in (bar, caption_label):
            target.mousePressEvent = self._title_press  # type: ignore[method-assign]
            target.mouseMoveEvent = self._title_move  # type: ignore[method-assign]
            target.mouseDoubleClickEvent = self._title_double_click  # type: ignore[method-assign]
        self.title_bar = bar
        return bar

    def _build_help_menu(self) -> QMenu:
        """The Help menu, which is the only way back to the FFmpeg offer.

        Before this existed, declining the first-run prompt meant closing and
        reopening the program, because the check ran before the window was shown
        and there was nothing else to click.  ``clear_cached_ffmpeg()`` had been
        written for the removal half of this and called from nowhere at all.
        """

        menu = QMenu("Help", self)
        updates = menu.addAction("Check for ClipDock &updates")
        updates.setStatusTip(f"Ask GitHub whether {RELEASES_URL} has something newer")
        updates.triggered.connect(self._check_for_updates)
        # The automatic check is a switch rather than a command, so it is
        # checkable and sits beside the thing it controls.  A switch buried in
        # a settings page nobody opens would mean the program contacts GitHub
        # for a person who never saw the control that allowed it; this way the
        # thing and its refusal are the same place.
        self._auto_update_action = menu.addAction("Check for updates &automatically")
        self._auto_update_action.setCheckable(True)
        self._auto_update_action.setChecked(self._update_check_enabled())
        self._auto_update_action.setToolTip(
            "Ask GitHub at most once a day whether a newer ClipDock has been "
            "published. Nothing is downloaded either way: the most you get is "
            "a notice in the corner naming the version."
        )
        self._auto_update_action.toggled.connect(self._set_update_check_enabled)
        menu.addSeparator()
        check = menu.addAction("&Check dependencies again")
        check.triggered.connect(self._check_dependencies_again)
        self._remove_ffmpeg_action = menu.addAction("&Remove the FFmpeg ClipDock installed")
        self._remove_ffmpeg_action.triggered.connect(self._remove_installed_ffmpeg)
        menu.addSeparator()
        menu.addAction("&About ClipDock").triggered.connect(self._show_about)
        # Enabled or not depends on what is on disk, which changes under the
        # program, so it is decided as the menu opens rather than once at build.
        menu.aboutToShow.connect(self._refresh_help_menu)
        # Also decided now, so the item is never briefly wrong before the first
        # open - and so a test can read the state without opening a menu.
        self._refresh_help_menu()
        return menu

    def _refresh_help_menu(self) -> None:
        self._remove_ffmpeg_action.setEnabled(installed_ffmpeg() is not None)

    def _check_for_updates(self) -> None:
        """Ask GitHub whether a newer release exists.  Report it; never fetch it.

        It runs through the same controller a download uses, so the window keeps
        responding to the mouse while it waits.  A frozen interface for eight
        seconds would be a worse answer than no update check at all, and the
        point of offering this at all is that the person chose to ask.

        Nothing is downloaded, and nothing is written.  The only thing this can
        produce is a version number to show and a page to open in a browser.
        """

        if self.controller.is_running:
            # A download owns the engine and the progress bar.  Interleaving a
            # check with it would fight over both, and the two questions have
            # nothing to do with each other.
            QMessageBox.information(
                self,
                "Already working",
                "ClipDock is busy with another task. Check for updates when it has finished.",
            )
            return
        self._set_busy(True, "Checking GitHub for a newer ClipDock…")
        self._reset_progress_display()
        self.status_bar.showMessage("Asking GitHub for the latest release…")

        def operation(progress: Any, cancel_check: Any) -> Any:
            return check_for_updates()

        self.controller.start(operation)

    def schedule_update_check(self) -> bool:
        """Arm the automatic update check, and say whether it was armed.

        Public because it is the one method here called from outside the
        window, by the application entry point after the window is shown.  The
        whole decision is made here and made once: the switch the person
        controls, the date that keeps it to one attempt a day, and whether
        anything else already owns the window.  It is its own method so the
        decision can be asserted directly instead of by waiting eight seconds
        for a timer no test should be allowed to let fire.
        """

        if not self._update_check_enabled():
            return False
        if self._update_check_ran_today():
            return False
        if self.controller.is_running:
            return False
        self._update_check_timer.start()
        return True

    def _run_scheduled_update_check(self) -> None:
        """The timer's callback: ask GitHub, and say nothing about it.

        Everything is re-checked here rather than trusted from
        ``schedule_update_check``, because eight seconds is time enough for
        the person to have started something else, and a check that raced a
        download would be asking for the fight ``_check_for_updates`` already
        refuses to pick.

        Silence is the default on purpose.  Nothing is written to the status
        bar, the progress bar is not touched, no dialog opens, and the date is
        recorded here rather than in the scheduler so that a window closed
        before the delay elapsed has not spent the day.
        """

        if not self.isVisible():
            # Closed, minimised to nothing, or never shown.  The timer is a
            # child of the window and is stopped by closeEvent as well, so
            # this is the belt to that pair of braces.
            return
        if not self._update_check_enabled() or self._update_check_ran_today():
            return
        if self.controller.is_running:
            # Not recorded: whoever is downloading decided this launch's
            # answer, so the next launch gets to ask again.
            return
        self._remember_update_check_started()
        self._update_check_automatic = True
        # No message.  The automatic check does not announce itself, because
        # saying nothing when there is nothing to say is the whole point of
        # it.  It still takes the busy state so the same rule that refuses a
        # second job applies here too.
        self._set_busy(True)

        def operation(progress: Any, cancel_check: Any) -> Any:
            return check_for_updates()

        self.controller.start(operation)

    def _report_update(
        self, result: UpdateCheck, *, automatic: bool = False
    ) -> None:
        """Say what the check found.  The only action offered is opening a page.

        An automatic check reports in exactly one case: there is a release to
        report.  Up to date, unreachable, rate-limited, or a tag nobody can
        compare are all reasons to say nothing at all, because the person did
        not ask and none of them is news.  A dialog that opened by itself to
        say "you are up to date" would be worse than no check, and a dialog
        that opened by itself to say "I could not reach GitHub" would be the
        connectivity problem presented as an application problem.
        """

        if automatic:
            if result.available:
                self._show_update_toast(result)
            return

        if result.available:
            self.status_bar.showMessage(f"ClipDock {result.latest} is available.", 10000)
            box = QMessageBox(self)
            box.setWindowTitle("A newer ClipDock is available")
            box.setIcon(QMessageBox.Icon.Information)
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(
                f"You are running ClipDock <b>{result.current}</b>.<br><br>"
                f"<b>{result.latest}</b> has been published."
            )
            box.setInformativeText(
                "ClipDock does not update itself, and it has not downloaded "
                "anything. The release page lists what changed and what it "
                "weighs; installing from there is your decision, not this "
                "program's."
            )
            open_button = box.addButton(
                "Open the release page", QMessageBox.ButtonRole.AcceptRole
            )
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(open_button)
            box.exec()
            if box.clickedButton() is open_button:
                QDesktopServices.openUrl(QUrl(result.release_url))
            return

        if result.up_to_date:
            self.status_bar.showMessage("ClipDock is up to date.", 6000)
            box = QMessageBox(self)
            box.setWindowTitle("ClipDock is up to date")
            box.setIcon(QMessageBox.Icon.Information)
            # Stated rather than left implicit: the check happened, and a reader
            # who did not ask for it is entitled to know what it did.
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(
                f"ClipDock <b>{result.current}</b> is the latest published release.<br><br>"
                "Nothing was downloaded."
            )
            box.exec()
            return

        self.status_bar.showMessage("Could not check for updates.", 8000)
        box = QMessageBox(self)
        box.setWindowTitle("Could not check for updates")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(result.reason or "The check did not finish.")
        box.setInformativeText(
            f"You are running ClipDock {result.current}. The published releases are at "
            f'<a href="{RELEASES_URL}">{RELEASES_URL}</a>.'
        )
        box.exec()

    def _show_update_toast(self, result: UpdateCheck) -> None:
        """Offer the release in the corner, and offer nothing else.

        The one automatic notice the program is allowed to produce, so the
        wording is doing the work of a conversation: it names the version, it
        says what is running, and it stops.  There is no "Update now", no
        exclamation mark, and nothing red - a newer release existing is a
        fact about a web page, and dressing it as an event would be the
        program implying urgency it has no grounds for.
        """

        toast = self._update_toast
        if toast is None:
            toast = self._build_update_toast()
            self._update_toast = toast
        latest = result.latest or "a newer release"
        self._update_toast_heading.setText(f"A newer ClipDock is available: {latest}")
        self._update_toast_detail.setText(
            f"You have {result.current}. {latest} has been published."
        )
        self._update_toast_url = result.release_url
        toast.show()
        toast.raise_()
        self._position_update_toast()

    def _build_update_toast(self) -> QFrame:
        """The notice itself, built the first time there is something to notice.

        On demand rather than during construction: a launch that found nothing
        would otherwise leave a hidden widget in every window ever opened.

        It offers exactly one thing - a web page - and says so.  There is no
        "Update" button because there is nothing behind one: ClipDock does not
        install itself, and a second button opening the page the first one
        describes would be pretending otherwise.
        """

        frame = QFrame(self)
        frame.setObjectName("updateToast")
        # Hiding before any content is laid out, so an empty frame never gets
        # a chance to be painted between construction and the show() below.
        frame.hide()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        heading_row = QHBoxLayout()
        heading_row.setSpacing(8)
        heading = QLabel("", frame)
        heading.setObjectName("updateToastHeading")
        self._update_toast_heading = heading
        heading_row.addWidget(heading, 1)
        close = QPushButton("✕", frame)
        close.setObjectName("updateToastClose")
        close.setFixedSize(22, 22)
        close.setToolTip("Dismiss this notice")
        close.clicked.connect(frame.hide)
        heading_row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading_row)

        detail = QLabel("", frame)
        detail.setObjectName("updateToastDetail")
        detail.setWordWrap(True)
        self._update_toast_detail = detail
        layout.addWidget(detail)

        note = QLabel(
            "ClipDock has not downloaded anything and does not update itself. "
            "What changed, and what it weighs, are on the release page.",
            frame,
        )
        note.setObjectName("updateToastNote")
        note.setWordWrap(True)
        layout.addWidget(note)

        open_button = QPushButton("Open the release page", frame)
        open_button.setObjectName("updateToastOpen")
        open_button.clicked.connect(self._open_update_toast_page)
        layout.addWidget(open_button, 0, Qt.AlignmentFlag.AlignLeft)
        return frame

    def _open_update_toast_page(self) -> None:
        """Open the release page, and only a page that really is one."""

        url = QUrl(self._update_toast_url)
        if url.scheme() != "https":
            # `check_for_updates` already replaces an `html_url` that is not
            # https with the releases page, so this is a second lock on the
            # same door rather than a repair for something observed.
            url = QUrl(RELEASES_URL)
        QDesktopServices.openUrl(url)

    def _position_update_toast(self) -> None:
        """Keep the notice in the corner it was promised.

        Above the status bar rather than over it: that line is what tells the
        person what the program is doing, and a notice that hid it would trade
        one piece of information for another.  The status bar lives inside the
        scrolling card, so its distance from the bottom edge belongs to the
        layout and is read back with `mapTo` instead of being assumed here.
        """

        toast = self._update_toast
        if toast is None or not hasattr(self, "status_bar"):
            return
        toast.adjustSize()
        margin = 16
        status_top = self.status_bar.mapTo(self, QPoint(0, 0)).y()
        top = status_top - toast.height() - margin
        # Clamped against the window as well as the status bar, so a short
        # window or a card scrolled past its own footer still shows the notice
        # on screen instead of half off the bottom of it.
        top = max(margin, min(top, self.height() - toast.height() - margin))
        toast.move(max(margin, self.width() - toast.width() - margin), top)

    def resizeEvent(self, event: Any) -> None:
        """Keep the notice where it was put when the window changes size."""

        super().resizeEvent(event)
        self._position_update_toast()

    def _check_dependencies_again(self) -> None:
        """Re-run the first-run check on demand, and say what it found either way.

        The first run stays silent when everything is present, because a dialog
        on every launch would make the program noisier the better behaved it was.
        That is the right default and the wrong answer here, where the whole
        point is that the user asked.
        """

        # Discovery is memoised, so a re-check has to forget the previous answer
        # or it would report whatever the first run found.
        forget_discovery()
        try:
            missing = missing_dependencies(find_ffmpeg)
        except Exception as error:
            get_logger().error("dependency_recheck_failed exception=%s", type(error).__name__)
            QMessageBox.warning(
                self,
                "Could not check",
                "ClipDock could not finish checking this computer. The application log has the detail.",
            )
            return
        if not missing:
            QMessageBox.information(
                self,
                "Everything ClipDock needs is here",
                f"{checked_devices()}\n\nNothing needs downloading.",
            )
            return
        # Modal, like the first-run one, because that is the same decision being
        # made again and it deserves the same attention.
        DependencyDialog(missing, self).exec()
        # Installing may have resolved FFmpeg as a side effect, so the memoised
        # search has to be dropped again or the engine keeps looking for it.
        forget_discovery()
        self.status_label.setText(
            "FFmpeg is installed." if find_ffmpeg() else "FFmpeg was not installed."
        )

    def _remove_installed_ffmpeg(self) -> None:
        """Remove the FFmpeg this application downloaded, and nothing else.

        Only ClipDock's own copy is touched.  A copy belonging to something else
        on the computer is found by the same device-wide search but is never
        deleted - it is not ours to delete.
        """

        target = installed_ffmpeg()
        if target is None:
            QMessageBox.information(
                self,
                "Nothing to remove",
                "ClipDock did not install FFmpeg on this computer, so there is nothing for it to remove. "
                "Any FFmpeg here belongs to another program and is left alone.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Remove the FFmpeg ClipDock installed?",
            f"This deletes:\n\n{target}\n\nand frees about {FFMPEG_INSTALLED_BYTES // 1_000_000} MB. "
            "Video merging, MP3 conversion, and cover art will stop working until FFmpeg is available again. "
            "FFmpeg installed by anything else is not touched.\n\nDelete it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        if clear_cached_ffmpeg():
            forget_discovery()
            self.status_label.setText("Removed the FFmpeg ClipDock installed.")
            QMessageBox.information(
                self,
                "Removed",
                f"Deleted {target}. Use Check dependencies again to put it back.",
            )
        else:
            QMessageBox.warning(
                self,
                "Could not remove it",
                "ClipDock could not delete that file. Close anything that might be using it and try again.",
            )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            "Downloads publicly accessible YouTube videos as MP4 or MP3, and saves "
            "their captions.<br><br>"
            f"yt-dlp {ytdlp_version() or 'version unavailable'}<br>"
            "FFmpeg is the only thing this program ever downloads, and only after asking."
            "<br><br>ClipDock does not update itself. Help - Check for ClipDock "
            f"updates asks GitHub whether a newer release exists and shows you "
            f'the page; it downloads nothing.<br><br><a href="{RELEASES_URL}">{RELEASES_URL}</a>'
            "<br><br>MIT licensed. Not affiliated with YouTube.",
        )

    @staticmethod
    def _window_button(parent: QWidget, glyph: str, tooltip: str) -> CaptionButton:
        """Build one caption button, sized to the shell's own metrics.

        The width is the shell's caption button width and the height is the
        title bar's full height, so the highlight covers the whole strip to the
        corner when hovered. Anything less and the button is a rectangle sitting
        in a title bar, which is the custom-control look being replaced.
        """
        button = CaptionButton(glyph, tooltip, parent)
        button.setObjectName("windowButton")
        button.setFixedSize(46, TITLE_BAR_HEIGHT)
        return button

    def _title_press(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        # Falling back to a manual move keeps the bar usable if the platform
        # refuses a system move, which it does in some remote sessions.
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _title_move(self, event: Any) -> None:
        if event.buttons() != Qt.MouseButton.LeftButton or self._drag_offset is None:
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def _title_double_click(self, event: Any) -> None:
        self._toggle_maximized()
        event.accept()

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event: Any) -> None:
        """Keep the restore button's label in step with the window state.

        The geometry of a maximized window is deliberately left alone.  Windows
        puts a maximized window's invisible frame just off the work area so the
        visible part exactly fills it, and forcing a geometry here would override
        that and drop the window back out of its maximized state, leaving the
        restore button lying about what the next click will do.
        """

        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            maximized = self.isMaximized()
            self.maximize_button.setToolTip("Restore" if maximized else "Maximize")
            self.maximize_button.setAccessibleName(self.maximize_button.toolTip())
            # The glyph has to change too, or the button claims to maximize a
            # window that is already maximized.
            self.maximize_button.set_glyph(caption.RESTORE if maximized else caption.MAXIMIZE)

    def nativeEvent(self, event_type: Any, message: Any) -> tuple[bool, int]:
        """Hand the window edges back to Windows so it stays resizable.

        A frameless window tells the shell it has no border, so without this it
        cannot be resized at all and no longer snaps.  Answering
        ``WM_NCHITTEST`` with the edge codes restores both.  Only the outermost
        few pixels are claimed: the interior stays ``HTCLIENT`` so ordinary
        widgets, including combo popups, keep receiving their own clicks.
        """

        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return super().nativeEvent(event_type, message)
        try:
            msg = wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):  # pragma: no cover - unexpected payload
            return super().nativeEvent(event_type, message)
        if msg.message != _WM_NCHITTEST:
            return super().nativeEvent(event_type, message)

        # lParam carries the cursor position in screen coordinates, packed as
        # two signed 16-bit values, negative when the cursor is left of or
        # above the primary screen.
        packed = int(msg.lParam)
        x = ctypes.c_short(packed & 0xFFFF).value
        y = ctypes.c_short((packed >> 16) & 0xFFFF).value
        point = QPoint(x, y)
        frame = self.frameGeometry()
        border = self._resize_border()
        near_left = abs(point.x() - frame.left()) <= border
        near_right = abs(point.x() - frame.right()) <= border
        near_top = abs(point.y() - frame.top()) <= border
        near_bottom = abs(point.y() - frame.bottom()) <= border

        if near_top and near_left:
            code = _HT_TOPLEFT
        elif near_top and near_right:
            code = _HT_TOPRIGHT
        elif near_bottom and near_left:
            code = _HT_BOTTOMLEFT
        elif near_bottom and near_right:
            code = _HT_BOTTOMRIGHT
        elif near_left:
            code = _HT_LEFT
        elif near_right:
            code = _HT_RIGHT
        elif near_top:
            code = _HT_TOP
        elif near_bottom:
            code = _HT_BOTTOM
        else:
            return super().nativeEvent(event_type, message)
        return True, code

    def _resize_border(self) -> int:
        """How many pixels at each edge act as a resize grip.

        Capped hard.  The system frame grows with DPI and on a scaled display
        reports 30-something pixels, which is taller than the title bar itself:
        the top edge would then swallow the whole caption and dragging the
        window would resize it instead.  A grip of a few pixels is all the
        shell needs to offer the same handles.
        """

        return max(6, min(_windows_resize_border(), _MAX_RESIZE_BORDER))

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(760, 540)
        self.resize(860, 700)
        # The window draws its own title bar rather than using the native one.
        # Two see-through themes used to make that mandatory, because a native
        # title bar is an opaque grey band; they are gone, but the frameless
        # window they introduced is kept, along with the drag, snap, and resize
        # behaviour it had to be given to be usable.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._drag_offset: QPoint | None = None

        root = QWidget(self)
        root.setObjectName("rootSurface")
        root_layout = QVBoxLayout(root)
        # The caption is no longer part of this layout, so the top margin has to
        # supply the breathing room it used to provide by sitting here.
        root_layout.setContentsMargins(24, 18, 24, 20)
        root_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(16)

        header_text_layout = QVBoxLayout()
        header_text_layout.setSpacing(3)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_text_layout.addWidget(title)

        subtitle = QLabel("Paste a YouTube video or playlist link, choose what to save, and download it locally.")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        subtitle.setWordWrap(True)
        subtitle.setMinimumHeight(22)
        header_text_layout.addWidget(subtitle)
        header_layout.addLayout(header_text_layout, 1)

        # The theme control must never stretch with the window.  On a maximized
        # window an expanding label/combo turned into a huge "Theme" badge, so
        # both widgets are pinned to their content width and the group keeps a
        # fixed size regardless of the available space.
        theme_container = QWidget()
        theme_container.setObjectName("ThemeContainer")
        theme_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        theme_layout = QHBoxLayout(theme_container)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        theme_layout.setSpacing(8)
        self.theme_label = QLabel("Theme")
        self.theme_label.setObjectName("themeLabel")
        self.theme_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.theme_label.setAccessibleName("Theme selector")
        self.theme_label.setToolTip("Theme selector")
        self.theme_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        theme_layout.addWidget(self.theme_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.theme_combo = SelectorComboBox()
        self.theme_combo.setObjectName("ThemeSelector")
        self.theme_combo.setAccessibleName("Color theme")
        self.theme_combo.setAccessibleDescription(
            f"Theme selector; {len(THEMES)} themes including VS Code Dark+. "
            "The chosen theme is remembered for the next launch."
        )
        for theme_id, label, description in THEMES:
            self.theme_combo.addItem(label, theme_id)
            index = self.theme_combo.count() - 1
            self.theme_combo.setItemData(index, description, Qt.ItemDataRole.ToolTipRole)
        self.theme_combo.setToolTip("Choose the application color theme")
        self.theme_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # The width is measured from the longest theme name so nothing is ever
        # elided, and it is still pinned: it never grows with the window.
        self._size_theme_combo()
        theme_layout.addWidget(self.theme_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.theme_container = theme_container
        header_layout.addWidget(theme_container, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        root_layout.addLayout(header_layout)

        input_group = QGroupBox("Video or playlist link")
        input_layout = QVBoxLayout(input_group)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a YouTube video or playlist URL")
        self.url_edit.setAccessibleName("YouTube video or playlist URL")
        self.url_edit.setClearButtonEnabled(True)
        self.fetch_button = QPushButton("Fetch details")
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.fetch_button)
        input_layout.addLayout(url_row)
        self.title_label = QLabel("No link selected")
        self.title_label.setObjectName("videoTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumHeight(24)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        input_layout.addWidget(self.title_label)
        root_layout.addWidget(input_group)

        options_group = QGroupBox("Download options")
        options_form = QFormLayout(options_group)
        self.mode_combo = SelectorComboBox()
        self.mode_combo.setAccessibleName("Download mode")
        self.mode_combo.setToolTip(
            "Video, audio, and thumbnail modes handle one video. Playlist mode downloads many videos "
            "sequentially and can save either the video or the audio track."
        )
        self.mode_combo.addItem("Video (MP4)", DownloadMode.VIDEO.value)
        self.mode_combo.addItem("Audio (MP3)", DownloadMode.AUDIO.value)
        self.mode_combo.addItem("Thumbnail", DownloadMode.THUMBNAIL.value)
        self.mode_combo.addItem("Subtitles (SRT/VTT)", DownloadMode.SUBTITLES.value)
        self.mode_combo.addItem("Playlist", DownloadMode.PLAYLIST.value)
        self.mode_combo.addItem("Batch queue", DownloadMode.QUEUE.value)
        options_form.addRow("Mode", self.mode_combo)

        self.playlist_media_combo = SelectorComboBox()
        self.playlist_media_combo.setAccessibleName("Playlist media")
        self.playlist_media_combo.setAccessibleDescription("Choose whether playlist downloads save video, audio, or subtitles")
        self.playlist_media_combo.setToolTip(
            "Choose whether each selected playlist video is saved as MP4 video, an MP3 audio track, "
            "or its caption file."
        )
        self.playlist_media_combo.addItem("Video only (MP4)", PlaylistMedia.VIDEO.value)
        self.playlist_media_combo.addItem("Audio only (MP3)", PlaylistMedia.AUDIO.value)
        self.playlist_media_combo.addItem("Subtitles only (SRT)", PlaylistMedia.SUBTITLES.value)
        self.playlist_media_label = QLabel("Playlist saves")
        options_form.addRow(self.playlist_media_label, self.playlist_media_combo)

        self.stream_preference_combo = SelectorComboBox()
        self.stream_preference_combo.setAccessibleName("Video stream preference")
        self.stream_preference_combo.setAccessibleDescription(
            "Choose the highest quality stream or the smallest file for each resolution"
        )
        for preference in StreamPreference:
            self.stream_preference_combo.addItem(preference.label, preference.value)
            self.stream_preference_combo.setItemData(
                self.stream_preference_combo.count() - 1, preference.tradeoff, Qt.ItemDataRole.ToolTipRole
            )
        # Replaced per-resolution by _sync_preference_availability, which knows
        # whether a smaller MP4 stream exists for the chosen quality.
        self.stream_preference_combo.setToolTip(
            "Applies to video mode and playlist video mode."
        )
        self.stream_preference_label = QLabel("Video stream")
        options_form.addRow(self.stream_preference_label, self.stream_preference_combo)

        self.quality_combo = SelectorComboBox()
        self.quality_combo.setToolTip(
            "Available MP4 resolutions and frame rates; playlist mode applies one choice to every video"
        )
        self.quality_label = QLabel("Video quality")
        options_form.addRow(self.quality_label, self.quality_combo)

        self.bitrate_combo = SelectorComboBox()
        for bitrate in (128, 192, 256, 320):
            self.bitrate_combo.addItem(f"{bitrate} kbps", bitrate)
        self.bitrate_label = QLabel("MP3 bitrate")
        self.bitrate_combo.setToolTip("Estimated MP3 sizes are calculated from the selected bitrate")
        options_form.addRow(self.bitrate_label, self.bitrate_combo)

        # Cover art embedding is a cosmetic feature - some users may prefer
        # smaller files or have players that don't handle ID3 APIC frames well.
        # The default is on, and the choice is remembered.
        self.embed_cover_checkbox = QCheckBox("Embed cover art in MP3")
        self.embed_cover_checkbox.setChecked(self._remembered_embed_cover())
        self.embed_cover_checkbox.setToolTip(
            "Attach the video's thumbnail as cover art inside the MP3 file. "
            "Uncheck to leave the MP3 without embedded artwork."
        )
        options_form.addRow("", self.embed_cover_checkbox)

        self.subtitle_format_combo = SelectorComboBox()
        self.subtitle_format_combo.setAccessibleName("Subtitle format")
        for subtitle_format in SubtitleFormat:
            self.subtitle_format_combo.addItem(subtitle_format.label, subtitle_format.value)
        self.subtitle_format_combo.setToolTip(
            "SRT is read by almost every player and editor. VTT is the web standard used by HTML5 video."
        )
        self.subtitle_format_label = QLabel("Subtitle format")
        options_form.addRow(self.subtitle_format_label, self.subtitle_format_combo)

        self.subtitle_source_combo = SelectorComboBox()
        self.subtitle_source_combo.setAccessibleName("Subtitle source")
        for source in SubtitleSource:
            self.subtitle_source_combo.addItem(source.label, source.value)
            self.subtitle_source_combo.setItemData(
                self.subtitle_source_combo.count() - 1,
                source.help_text,
                Qt.ItemDataRole.ToolTipRole,
            )
        self.subtitle_source_combo.setToolTip(SubtitleSource.PREFERRED.help_text)
        self.subtitle_source_label = QLabel("Captions from")
        options_form.addRow(self.subtitle_source_label, self.subtitle_source_combo)

        self.subtitle_language_combo = SelectorComboBox()
        self.subtitle_language_combo.setAccessibleName("Subtitle language")
        self.subtitle_language_combo.setToolTip(
            "Languages advertised for this video. One file is written per selected language."
        )
        self.subtitle_language_label = QLabel("Subtitle language")
        options_form.addRow(self.subtitle_language_label, self.subtitle_language_combo)

        # Sizes live in the quality and bitrate lists, beside the choice they
        # belong to.  This line only carries what those lists cannot: how many
        # caption files a run writes, how a batch was summarised, and what to
        # do before details have been read.  It hides itself when it has
        # nothing to add, so no empty gap is left behind.
        self.summary_label = QLabel()
        self.summary_label.setObjectName("summaryLine")
        self.summary_label.setWordWrap(True)
        self.summary_label.setMinimumHeight(20)
        options_form.addRow(self.summary_label)
        root_layout.addWidget(options_group)

        self.playlist_group = QGroupBox("Playlist videos")
        playlist_layout = QVBoxLayout(self.playlist_group)
        playlist_layout.setSpacing(8)

        playlist_header = QHBoxLayout()
        playlist_header.setSpacing(8)
        self.playlist_selection_label = QLabel("Choose which videos to download:")
        self.playlist_selection_label.setWordWrap(True)
        playlist_header.addWidget(self.playlist_selection_label, 1)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.setToolTip("Select every listed video")
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.setToolTip("Clear the current video selection")
        playlist_header.addWidget(self.select_all_button, 0)
        playlist_header.addWidget(self.select_none_button, 0)
        playlist_layout.addLayout(playlist_header)

        self.playlist_table = QTableWidget(0, PLAYLIST_COLUMN_COUNT)
        self.playlist_table.setObjectName("PlaylistTable")
        self.playlist_table.setAccessibleName("Playlist video selection")
        self.playlist_table.setColumnCount(PLAYLIST_COLUMN_COUNT)
        self.playlist_table.setHorizontalHeaderLabels(
            ["Save", "Video", "Length", f"Size at {PlaylistMedia.VIDEO.format_label}"]
        )
        self.playlist_table.verticalHeader().setVisible(False)
        self.playlist_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.playlist_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.playlist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.playlist_table.setWordWrap(False)
        self.playlist_table.setAlternatingRowColors(True)
        self.playlist_table.setMinimumHeight(170)
        header_view = self.playlist_table.horizontalHeader()
        header_view.setSectionResizeMode(PLAYLIST_COLUMN_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(PLAYLIST_COLUMN_TITLE, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(
            PLAYLIST_COLUMN_LENGTH,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        header_view.setSectionResizeMode(PLAYLIST_COLUMN_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        playlist_layout.addWidget(self.playlist_table, 1)

        self.playlist_summary_label = QLabel("Fetch a playlist to choose videos.")
        self.playlist_summary_label.setObjectName("playlistSummary")
        self.playlist_summary_label.setWordWrap(True)
        self.playlist_summary_label.setMinimumHeight(24)
        playlist_layout.addWidget(self.playlist_summary_label)
        root_layout.addWidget(self.playlist_group)

        self.queue_group = QGroupBox("Batch queue")
        queue_layout = QVBoxLayout(self.queue_group)
        queue_layout.setSpacing(8)

        self.queue_edit = QPlainTextEdit()
        self.queue_edit.setObjectName("QueueEditor")
        self.queue_edit.setAccessibleName("Links to download one after another")
        self.queue_edit.setPlaceholderText(
            "Paste one YouTube link per line.\n"
            "Blank lines and duplicate links are ignored.\n"
            "A link that fails does not stop the rest."
        )
        self.queue_edit.setToolTip(
            "Each line is downloaded in turn. A link that fails is reported and the queue "
            "continues with the next one."
        )
        self.queue_edit.setTabChangesFocus(True)
        self.queue_edit.setMinimumHeight(110)
        queue_layout.addWidget(self.queue_edit)

        queue_header = QHBoxLayout()
        queue_header.setSpacing(8)
        self.queue_summary_label = QLabel("Add links above to build a queue.")
        self.queue_summary_label.setObjectName("playlistSummary")
        self.queue_summary_label.setWordWrap(True)
        queue_header.addWidget(self.queue_summary_label, 1)
        self.queue_media_combo = SelectorComboBox()
        self.queue_media_combo.setAccessibleName("Queue saves")
        self.queue_media_combo.setToolTip("What each queued link is saved as")
        for queue_mode in _QUEUE_MODES:
            self.queue_media_combo.addItem(QUEUE_MEDIA_LABELS[queue_mode], queue_mode.value)
        queue_header.addWidget(self.queue_media_combo, 0)
        self.queue_clear_button = QPushButton("Clear")
        self.queue_clear_button.setToolTip("Remove every link from the queue")
        queue_header.addWidget(self.queue_clear_button, 0)
        queue_layout.addLayout(queue_header)

        self.queue_table = QTableWidget(0, QUEUE_COLUMN_COUNT)
        self.queue_table.setObjectName("QueueTable")
        self.queue_table.setAccessibleName("Queue progress")
        self.queue_table.setColumnCount(QUEUE_COLUMN_COUNT)
        self.queue_table.setHorizontalHeaderLabels(["#", "Link", "Title", "Size"])
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setWordWrap(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setMinimumHeight(120)
        queue_header_view = self.queue_table.horizontalHeader()
        queue_header_view.setSectionResizeMode(QUEUE_COLUMN_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        queue_header_view.setSectionResizeMode(QUEUE_COLUMN_LINK, QHeaderView.ResizeMode.Stretch)
        queue_header_view.setSectionResizeMode(QUEUE_COLUMN_TITLE, QHeaderView.ResizeMode.Stretch)
        queue_header_view.setSectionResizeMode(QUEUE_COLUMN_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        queue_layout.addWidget(self.queue_table)
        root_layout.addWidget(self.queue_group)

        destination_group = QGroupBox("Destination")
        destination_layout = QVBoxLayout(destination_group)
        destination_row = QHBoxLayout()
        self.destination_edit = QLineEdit(str(self._last_directory))
        self.destination_edit.setPlaceholderText("Choose a folder")
        browse_button = QPushButton("Browse")
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(browse_button)
        destination_layout.addLayout(destination_row)
        root_layout.addWidget(destination_group)
        self.browse_button = browse_button

        progress_group = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_group)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(24)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.progress_bar)
        root_layout.addWidget(progress_group)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        # These two only become usable once a download has produced a file, so
        # they sit on the left of the row and start disabled.
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setObjectName("OpenFolder")
        self.open_folder_button.setToolTip("Open the download folder in the file manager")
        self.open_folder_button.setEnabled(False)
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("Play")
        self.play_button.setToolTip("Open the downloaded file in the default player")
        self.play_button.setEnabled(False)
        # Retry rescues whatever the last playlist or batch job could not save.  It
        # reads as a plain "Retry" until there is actually something to retry,
        # at which point it names itself after the failed items.
        self.retry_button = QPushButton("Retry")
        self.retry_button.setObjectName("Retry")
        self.retry_button.setToolTip(
            "Run the last playlist or batch download again"
        )
        self.retry_button.setEnabled(False)
        # Resume continues a download this session stopped.  It is *hidden*
        # rather than disabled until then, which is a stricter rule than Retry
        # follows and the one this control needs: a Resume that is present but
        # dead is a promise the app cannot keep, because continuing a download
        # needs the request that was running and nothing survives a restart.
        self.resume_button = QPushButton("Resume")
        self.resume_button.setObjectName("Resume")
        self.resume_button.setToolTip(
            "Continue a download that was stopped"
        )
        self.resume_button.setEnabled(False)
        self.resume_button.setVisible(False)
        # Pause is a stop, not a pause.  yt-dlp has no pause primitive to call,
        # so what this does is cancel the job gently and leave every piece it
        # has already written where it is; whether the continuation then picks
        # up at the byte it reached is not established, and neither the tooltip
        # nor the documentation says that it does.
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("Pause")
        self.pause_button.setToolTip(
            "Stop the download and keep what has arrived so far. Not a true pause: "
            "the file continues from its saved pieces rather than from the exact byte."
        )
        self.pause_button.setEnabled(False)
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("Download")
        self.download_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        action_row.addWidget(self.open_folder_button)
        action_row.addWidget(self.play_button)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.resume_button)
        action_row.addStretch(1)
        action_row.addWidget(self.pause_button)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.download_button)
        root_layout.addLayout(action_row)

        root.setMinimumWidth(680)
        scroll_area = QScrollArea()
        scroll_area.setObjectName("ContentScroll")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.viewport().setObjectName("contentViewport")
        scroll_area.setWidget(root)
        # The scroll area fills the window, so the card fills it too and reaches the
        # window's edges: with every palette opaque there is no margin to inset,
        # and a card that stopped short of the edge would leave a bare strip of
        # shell down each side.
        shell = QWidget()
        shell.setObjectName("windowShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        # The caption sits outside the scroll area. Inside it, the scroll bar ran
        # the full height of the window right beside the caption buttons, and the
        # caption itself scrolled away with the content - both wrong for a window
        # frame, which is fixed furniture by definition. Above the scroll area it
        # does what a caption does: stays put, and leaves the scroll bar to the
        # content it belongs to.
        shell_layout.addWidget(self._build_title_bar())
        shell_layout.addWidget(scroll_area, 1)
        # Kept as an attribute because the opaque-pixel test asserts that the
        # shell has no margin, which is what lets the card reach the edges.
        self._shell_layout = shell_layout
        self._scroll_area = scroll_area
        self.setCentralWidget(shell)
        # The status bar lives inside the card rather than in QMainWindow's own
        # dock area, so it is styled and sized as part of the same surface the
        # rest of the app is drawn on.
        self.status_bar = QStatusBar(root)
        self.status_bar.setObjectName("statusLineBar")
        self.status_bar.setSizeGripEnabled(False)
        root_layout.addWidget(self.status_bar)
        self.status_bar.showMessage("Ready")
        # The window is complete, so the first theme application can be the
        # remembered one.
        self._apply_theme(self._theme)

    def _apply_theme(self, theme: str) -> None:
        if theme not in THEME_IDS:
            theme = DEFAULT_THEME
        self._theme = theme
        self._theme_is_dark = is_dark(theme)
        colors = self._theme_colors(theme)

        if hasattr(self, "theme_combo"):
            index = self.theme_combo.findData(theme)
            if index >= 0 and self.theme_combo.currentIndex() != index:
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(index)
                self.theme_combo.blockSignals(False)

        palette = QPalette()
        role_colors = {
            QPalette.ColorRole.Window: colors["window"],
            QPalette.ColorRole.WindowText: colors["text"],
            QPalette.ColorRole.Base: colors["input"],
            QPalette.ColorRole.AlternateBase: colors["surface"],
            QPalette.ColorRole.Text: colors["text"],
            QPalette.ColorRole.Button: colors["button"],
            QPalette.ColorRole.ButtonText: colors["text"],
            QPalette.ColorRole.Highlight: colors["primary"],
            QPalette.ColorRole.HighlightedText: colors["primary_text"],
            QPalette.ColorRole.ToolTipBase: colors["tooltip"],
            QPalette.ColorRole.ToolTipText: colors["tooltip_text"],
            QPalette.ColorRole.PlaceholderText: colors["muted"],
            QPalette.ColorRole.Link: colors["primary"],
        }
        for role, color in role_colors.items():
            palette.setColor(role, QColor(color))

        # Apply the palette and stylesheet to the application as well as this
        # window so native combo-box popups and message boxes remain readable.
        stylesheet = self._stylesheet(colors)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(palette)
            app.setStyleSheet(stylesheet)
        self.setPalette(palette)
        self.setStyleSheet(stylesheet)
        self._apply_native_chrome(colors)
        # The chevron is painted, not styled, so every list has to be told the
        # theme's colours or it would keep whatever it guessed at construction.
        for combo in self._selector_combos():
            combo.set_chevron_colors(colors["text"], colors["disabled_text"])
        # The caption glyphs are painted too, for the same reason: they have no
        # text for a stylesheet to colour.  The surface colour is only needed by
        # the restore glyph, to knock its back square out from behind the front
        # one; it is the title bar's, because that is what sits behind them.
        for button in self._caption_buttons():
            button.set_caption_colors(
                colors["text"], colors["text"], colors["text"], colors["panel"]
            )
        # The combo is bordered by the stylesheet, so its true gutter is only
        # known once that is in place.  Measuring before it would leave the
        # widest name a couple of pixels short of fitting.
        if hasattr(self, "theme_combo"):
            self._size_theme_combo()

    def _selector_combos(self) -> list[SelectorComboBox]:
        """Every drop-down in the window.

        Collected from the widget tree rather than from an attribute list, so a
        combo added later cannot be missed: a selector left without the theme's
        chevron colour is the one thing that would look wrong on screen.
        """

        return list(self.findChildren(SelectorComboBox))

    def _caption_buttons(self) -> list[CaptionButton]:
        """Every painted button on the bar, in the order they appear on it.

        The overflow trigger is included even though it is not a caption control
        and never moves the window: its mark is painted the same way, so leaving
        it out would leave it in the colour the theme happened to start with.
        """

        return [
            self.help_button,
            self.minimize_button,
            self.maximize_button,
            self.close_button,
        ]

    def _apply_native_chrome(self, colors: dict[str, str]) -> None:
        """Tell Windows to round this window's corners and draw its border.

        ``DWMWA_WINDOW_CORNER_PREFERENCE`` and ``DWMWA_BORDER_COLOR`` are the
        compositor's own rounded-corner and border support.  They are used in
        preference to a Qt translucency trick because they clip the window
        rather than making it see-through, so the surface stays fully opaque
        and text can never end up drawn over the desktop.

        Both attributes arrived in Windows 11.  On anything older the call
        simply fails and the window keeps its square corners, so the result is
        not checked: there is nothing to fall back to within this program.
        """

        handle = self.windowHandle()
        if handle is None:
            # No native window yet; showEvent applies the chrome once there is.
            return
        try:
            hwnd = wintypes.HWND(int(handle.winId()))
            dwnm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
            # DWMWA_WINDOW_CORNER_PREFERENCE.  2 is DWMWCP_ROUND, the small
            # radius Windows 11 uses for ordinary application windows.
            preference = wintypes.DWORD(2)
            dwnm.DwmSetWindowAttribute(
                hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(preference), ctypes.sizeof(preference)
            )
            # DWMWA_BORDER_COLOR.  COLORREF is 0x00BBGGRR, so the channels are
            # written from the other end: Windows reads them as B, G, R.
            border = QColor(colors["border"])
            colour_ref = wintypes.DWORD(
                border.blue() | (border.green() << 8) | (border.red() << 16)
            )
            dwnm.DwmSetWindowAttribute(
                hwnd, _DWMWA_BORDER_COLOR, ctypes.byref(colour_ref), ctypes.sizeof(colour_ref)
            )
        except (AttributeError, OSError, TypeError, ValueError):
            # Not Windows, or a Windows build without the attributes.
            return

    def showEvent(self, event: Any) -> None:
        """Apply the window rounding once the native window actually exists."""

        super().showEvent(event)
        self._apply_native_chrome(self._theme_colors(self._theme))

    @staticmethod
    def _theme_colors(theme: str) -> dict[str, str]:
        return theme_palette(theme)

    def _size_theme_combo(self) -> None:
        """Pin the theme combo to the width its list needs.

        There is no upper clamp: a theme name is never elided, so the button is
        always as wide as the list needs.  The size policy stays fixed, so this
        width never changes with the window.
        """

        self.theme_combo.setFixedWidth(self._theme_combo_target())
        self.theme_label.setFixedHeight(self.theme_combo.sizeHint().height())

    def _theme_combo_target(self) -> int:
        """The width that fits the longest theme name, in pixels.

        A combo's size hint is its widest entry plus the frame, which is exactly
        what is wanted, so the hint is the answer rather than something to
        reconstruct.  The size-adjust policy is stated instead of assumed: under
        the other setting the hint is measured from the *current* entry and would
        be too narrow for the rest of the list, which is the elision this
        method exists to prevent.
        """

        self.theme_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        # The floor keeps a short list from looking cramped.
        return max(96, self.theme_combo.sizeHint().width())

    def _stylesheet(self, colors: dict[str, str]) -> str:
        return f"""
            QMainWindow {{
                /* Clear, so a see-through theme's margins really do show the
                   desktop.  The card below carries every pixel of content. */
                background: transparent;
                color: {colors['text']};
            }}
            QDialog {{
                /* A dialog is its own small window, not a card floating in
                   this one, so it needs a fill of its own. */
                background: {colors['panel']};
                color: {colors['text']};
            }}
            QWidget {{
                color: {colors['text']};
            }}
            QGroupBox {{
                background: {colors['surface']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                border-radius: 8px;
                font-weight: 600;
                margin-top: 10px;
                padding: 12px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: {colors['text']};
            }}
            QLabel#title {{
                color: {colors['text']};
                font-size: 24px;
                font-weight: 700;
            }}
            QLabel#videoTitle {{
                color: {colors['text']};
                font-weight: 600;
            }}
            QLabel#subtitle,
            QLabel#status,
            QLabel#fieldLabel,
            QLabel#summaryLine {{
                color: {colors['muted']};
            }}
            QLabel#themeLabel {{
                background: {colors['primary']};
                color: {colors['primary_text']};
                border: 1px solid {colors['primary']};
                border-radius: 6px;
                font-weight: 700;
                padding: 5px 10px;
            }}
            QLabel#summaryLine {{
                color: {colors['text']};
                font-weight: 600;
            }}
            QLabel#playlistSummary {{
                color: {colors['muted']};
            }}
            QWidget#ThemeContainer {{
                background: transparent;
            }}
            QWidget#windowShell {{
                /* Clear so the card and the caption strip, not the shell, decide
                   what the window looks like.  Both of those now paint the
                   surface themselves - the strip because it is outside the card -
                   so nothing shows through here. */
                background: transparent;
            }}
            QWidget#rootSurface {{
                /* The card.  Everything the user reads is painted on this. */
                background: {colors['panel']};
            }}
            QWidget#contentViewport {{
                /* The scroll area's viewport is a plain QWidget that fills itself
                   from the palette by default, which would be a differently
                   colored band inside the card.  It is named so this rule can
                   clear it without also matching the card: the card is the
                   viewport's child, and a descendant selector naming it would
                   outrank the card's own rule. */
                background: transparent;
            }}
            QFrame#titleBar {{
                /* Opaque, and the card's own colour. The caption used to live
                   inside the scrolling card and could be transparent; now that
                   it is a fixed strip above the scroll area it is the only thing
                   painting the top of the window, so it has to paint the
                   surface itself. Matching the card keeps the two reading as one
                   continuous face rather than as a title bar and a body. */
                background: {colors['panel']};
            }}
            QStatusBar#statusLineBar {{
                background: transparent;
                color: {colors['muted']};
            }}
            QStatusBar#statusLineBar::item {{
                border: none;
            }}
            QLabel#titleBarCaption {{
                color: {colors['muted']};
                font-weight: 600;
            }}
            QPushButton#windowButton,
            QPushButton#windowButtonMaximize,
            QPushButton#windowButtonClose {{
                /* No border, no resting surface, no rounding.  These are the
                   window frame, not application buttons, and every cue that says
                   "button" - a box, a fill, rounded corners - is what made the
                   previous version read as three typed labels.  The glyph itself
                   is painted by CaptionButton, so there is no text here at all;
                   what is left to style is only the background the shell paints
                   on hover and press.

                   All three names are listed because the close button carries its
                   own hover colour below, and a selector naming just
                   #windowButton would match the minimize button alone. */
                background: transparent;
                border: none;
                border-radius: 0px;
            }}
            QPushButton#windowButton:hover,
            QPushButton#windowButtonMaximize:hover {{
                /* Edge to edge with no rounding, the way a caption highlight is
                   drawn.  A themed accent is not used: the shell's highlight is
                   a neutral lightening of the surface, and colouring it would
                   make the frame look like it belonged to the app's palette. */
                background: {colors['button_hover']};
            }}
            QPushButton#windowButton:pressed,
            QPushButton#windowButtonMaximize:pressed {{
                background: {colors['button']};
            }}
            QPushButton#windowButtonClose:hover {{
                /* The shell's own close-button accent, not a themed red: see
                   gui/caption.py.  The glyph turns white on it, which is why
                   the colour is not taken from the palette. */
                background: {caption.CLOSE_HOVER};
            }}
            QPushButton#windowButtonClose:pressed {{
                background: {caption.CLOSE_HOVER_PRESSED};
            }}
            /* Styled like a caption button rather than a form control: same
               metrics, same full-height hover, no border and no resting fill.
               No colour and no font-size, because the mark is painted rather
               than being text - `set_caption_colors` supplies it, the same as
               the three window controls.  The menu indicator is hidden because
               this opens a menu on press, which is what a menu trigger does,
               and an arrow would suggest a separate control with its own
               target. */
            QPushButton#helpButton {{
                background: transparent;
                border: none;
                border-radius: 0px;
            }}
            QPushButton#helpButton:hover {{
                background: {colors['button_hover']};
            }}
            QPushButton#helpButton:pressed {{
                background: {colors['button_hover']};
            }}
            QPushButton#helpButton:menu-indicator {{
                image: none;
                width: 0px;
            }}
            QFrame#updateToast {{
                /* The corner notice for a release that already exists.  It sits
                   on top of the card, so it needs a fill and a border of its
                   own or it reads as text that appeared out of nowhere.  The
                   wording on it is deliberately flat: a release being available
                   is a fact, not an event. */
                background: {colors['surface']};
                border: 1px solid {colors['border']};
                border-radius: 10px;
            }}
            QLabel#updateToastHeading {{
                color: {colors['text']};
                font-weight: 700;
            }}
            QLabel#updateToastDetail {{
                color: {colors['text']};
            }}
            QLabel#updateToastNote {{
                color: {colors['muted']};
            }}
            QPushButton#updateToastClose {{
                /* A drawn cross rather than a native one, so it reads as part
                   of the notice instead of as a window button. */
                background: transparent;
                border: none;
                border-radius: 4px;
                color: {colors['muted']};
                min-height: 18px;
                padding: 0 4px;
            }}
            QPushButton#updateToastClose:hover {{
                background: {colors['button_hover']};
                color: {colors['text']};
            }}
            QPushButton#updateToastOpen {{
                /* Shown at the same weight as Download so it is clearly the
                   thing to press, and coloured like it for the same reason -
                   but it opens a web page and nothing else. */
                background: {colors['primary']};
                color: {colors['primary_text']};
                border: 1px solid {colors['primary']};
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
            }}
            QPushButton#updateToastOpen:hover {{
                background: {colors['primary_hover']};
                border-color: {colors['primary_hover']};
            }}
            QPushButton#updateToastOpen:pressed {{
                background: {colors['primary_hover']};
                color: {colors['primary_text']};
            }}
            QTableWidget#PlaylistTable,
            QTableWidget#QueueTable {{
                background: {colors['input']};
                alternate-background-color: {colors['surface']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                border-radius: 6px;
                gridline-color: {colors['border']};
                selection-background-color: {colors['primary']};
                selection-color: {colors['primary_text']};
            }}
            QTableWidget#PlaylistTable::item,
            QTableWidget#QueueTable::item {{
                padding: 4px 6px;
            }}
            QTableWidget#PlaylistTable::item:selected,
            QTableWidget#QueueTable::item:selected {{
                background: {colors['primary']};
                color: {colors['primary_text']};
            }}
            QTableWidget#QueueTable::item:disabled {{
                color: {colors['disabled_text']};
            }}
            QCheckBox {{
                color: {colors['text']};
                spacing: 8px;
                padding: 2px 4px;
            }}
            QCheckBox:disabled {{
                color: {colors['disabled_text']};
            }}
            QScrollArea#ContentScroll {{
                /* The card behind it already paints the fill; the scroll area
                   must stay clear or it covers the card's rounded corners. */
                background: transparent;
                border: none;
            }}
            QLineEdit,
            QPlainTextEdit,
            QComboBox {{
                background: {colors['input']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                border-radius: 6px;
                min-height: 22px;
                padding: 5px 8px;
                selection-background-color: {colors['primary']};
                selection-color: {colors['primary_text']};
            }}
            QLineEdit:hover,
            QPlainTextEdit:hover,
            QComboBox:hover {{
                border-color: {colors['primary']};
            }}
            QLineEdit:focus,
            QPlainTextEdit:focus,
            QComboBox:focus {{
                border: 2px solid {colors['primary']};
                padding: 4px 7px;
            }}
            QLineEdit:disabled,
            QPlainTextEdit:disabled,
            QComboBox:disabled {{
                background: {colors['disabled_bg']};
                color: {colors['disabled_text']};
                border-color: {colors['border']};
            }}
            QComboBox::drop-down {{
                /* The chevron inside SelectorComboBox is painted rather than
                   styled, so there is no image to reserve space for here.  The
                   width still matches the gutter that painter centres it in. */
                border: none;
                width: 26px;
                subcontrol-origin: padding;
                subcontrol-position: center right;
            }}
            QComboBox QAbstractItemView {{
                background: {colors['surface']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                outline: none;
                selection-background-color: {colors['primary']};
                selection-color: {colors['primary_text']};
            }}
            QComboBox QAbstractItemView::item {{
                color: {colors['text']};
                min-height: 24px;
                padding: 2px 6px;
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: {colors['primary']};
                color: {colors['primary_text']};
            }}
            QPushButton {{
                background: {colors['button']};
                color: {colors['text']};
                border: 1px solid {colors['border']};
                border-radius: 6px;
                min-height: 30px;
                padding: 0 14px;
            }}
            QPushButton:hover {{
                background: {colors['button_hover']};
                border-color: {colors['primary']};
            }}
            QPushButton:pressed {{
                background: {colors['primary_hover']};
                color: {colors['primary_text']};
            }}
            QPushButton:disabled {{
                background: {colors['disabled_bg']};
                color: {colors['disabled_text']};
                border-color: {colors['border']};
            }}
            QPushButton#Download {{
                background: {colors['primary']};
                color: {colors['primary_text']};
                font-weight: 700;
                border: 1px solid {colors['primary']};
            }}
            QPushButton#Download:hover {{
                background: {colors['primary_hover']};
                border-color: {colors['primary_hover']};
            }}
            QPushButton#Download:pressed {{
                background: {colors['primary_hover']};
                color: {colors['primary_text']};
            }}
            QPushButton#Download:disabled {{
                background: {colors['disabled_bg']};
                color: {colors['disabled_text']};
                border-color: {colors['border']};
            }}
            QProgressBar {{
                background: {colors['track']};
                color: {colors['progress_text']};
                border: 1px solid {colors['border']};
                border-radius: 6px;
                min-height: 20px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {colors['progress']};
                border-radius: 5px;
            }}
            QStatusBar {{
                background: {colors['surface']};
                color: {colors['muted']};
            }}
            QStatusBar::item {{
                border: none;
            }}
            QToolTip {{
                background: {colors['tooltip']};
                color: {colors['tooltip_text']};
                border: 1px solid {colors['border']};
                padding: 4px;
            }}
            QScrollBar:vertical {{
                /* Painted in the card's own colour rather than left clear: the
                   card is narrower than the viewport by exactly the bar's width,
                   so a clear groove would let the desktop show through the
                   window.  There is deliberately no QSS margin here - Qt does
                   not fill a scrollbar's margin area with its background, so any
                   margin would become a clear stripe down the edge of the window.
                   The inset comes from the handle's own margin instead, which Qt
                   does honour.

                   14px rather than the previous 16: the handle is a narrow pill
                   floating inside this gutter, and a wider gutter only adds
                   empty space between the pill and the content it scrolls. */
                background: {colors['panel']};
                border: none;
                width: 14px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                /* Muted rather than border: the border colour sits between the
                   page and the surface in most themes, which is what made the
                   handle so hard to see.  Muted is the contrast step the
                   palette already uses for text that has to be readable.

                   The margin insets the pill from both edges of the gutter, and
                   the radius is half the resulting width, so the handle is a
                   floating capsule that never touches anything. That is the
                   whole of the "modern" look here: a stock bar is a solid block
                   welded to the gutter with square ends. */
                background: {colors['muted']};
                border: none;
                /* 14px gutter less 5px each side leaves a 4px pill. */
                border-radius: 2px;
                margin: 6px 5px;
                min-height: 40px;
            }}
            QScrollBar::handle:vertical:hover {{
                /* Widens towards the pointer on hover, the way a modern
                   overlay scroll bar behaves.  Still fully rounded at 8px. */
                background: {colors['primary']};
                border-radius: 4px;
                margin: 3px 3px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                /* The arrow buttons at each end are the other half of what made
                   this look like a stock scroll bar; they are removed so the
                   handle runs the full height. */
                background: transparent;
                border: none;
                height: 0;
            }}
            QScrollBar:horizontal {{
                /* Same two reasons as the vertical one: the groove sits outside
                   the scroll area's own widget so it carries the fill, and a
                   margin would leave a clear stripe along the window edge. */
                background: {colors['panel']};
                border: none;
                height: 14px;
                margin: 0;
            }}
            QScrollBar::handle:horizontal {{
                /* The vertical bar's values transposed: a 4px capsule floating
                   in a 14px gutter, widening to 8px on hover. */
                background: {colors['muted']};
                border: none;
                border-radius: 2px;
                margin: 5px 6px;
                min-width: 40px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {colors['primary']};
                border-radius: 4px;
                margin: 3px 3px;
            }}
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                background: transparent;
                border: none;
                width: 0;
            }}
        """

    def _connect_signals(self) -> None:
        self.url_edit.returnPressed.connect(self._fetch_details)
        self.url_edit.textChanged.connect(self._url_changed)
        self.fetch_button.clicked.connect(self._fetch_details)
        self.browse_button.clicked.connect(self._choose_destination)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.playlist_media_combo.currentIndexChanged.connect(self._playlist_media_changed)
        self.stream_preference_combo.currentIndexChanged.connect(self._preference_changed)
        self.quality_combo.currentIndexChanged.connect(self._update_summary)
        self.bitrate_combo.currentIndexChanged.connect(self._update_summary)
        self.subtitle_source_combo.currentIndexChanged.connect(self._subtitle_source_changed)
        self.subtitle_format_combo.currentIndexChanged.connect(self._update_summary)
        self.subtitle_language_combo.currentIndexChanged.connect(self._update_summary)
        self.playlist_table.itemChanged.connect(self._playlist_item_changed)
        self.select_all_button.clicked.connect(lambda: self._set_all_playlist_rows(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_playlist_rows(False))
        self.queue_edit.textChanged.connect(self._queue_links_changed)
        self.queue_media_combo.currentIndexChanged.connect(self._queue_media_changed)
        self.queue_clear_button.clicked.connect(self._clear_queue)
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        self.embed_cover_checkbox.toggled.connect(self._embed_cover_changed)
        self.download_button.clicked.connect(self._start_download)
        self.cancel_button.clicked.connect(self._cancel_job)
        self.pause_button.clicked.connect(self._pause_job)
        self.open_folder_button.clicked.connect(self._open_last_folder)
        self.play_button.clicked.connect(self._play_last_file)
        self.retry_button.clicked.connect(self._retry_failed_items)
        self.resume_button.clicked.connect(self._resume_paused_job)
        self.controller.progress.connect(self._on_progress)
        self.controller.succeeded.connect(self._on_job_succeeded)
        self.controller.failed.connect(self._on_job_failed)
        self.controller.cancelled.connect(self._on_job_cancelled)
        self.controller.finished.connect(self._on_job_thread_finished)

    def _theme_changed(self, index: int) -> None:
        theme = self.theme_combo.itemData(index)
        if not isinstance(theme, str):
            return
        self._apply_theme(theme)
        # Remembered so the next launch opens on the theme the user picked.  A
        # failure here is deliberately ignored: the choice is already applied
        # on screen, and not being able to write one file is not worth an
        # error dialog.
        write_setting(THEME_SETTING_KEY, theme)

    def _embed_cover_changed(self, checked: bool) -> None:
        # Remember the user's choice so the next launch respects it.
        write_setting(EMBED_COVER_SETTING_KEY, "1" if checked else "0")

    def _choose_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose download folder", self.destination_edit.text())
        if selected:
            self.destination_edit.setText(selected)
            self._last_directory = Path(selected)

    def _fetch_details(self) -> None:
        if self.controller.is_running:
            return
        raw_url = self.url_edit.text().strip()
        mode = self._selected_mode()
        if mode is DownloadMode.QUEUE:
            # A batch does not need its links read before it starts; each link is
            # probed by the job itself, so the Fetch button is not needed.
            self._rebuild_queue_table()
            self._update_summary()
            self._update_download_button()
            return
        try:
            if mode is DownloadMode.PLAYLIST:
                normalize_youtube_playlist_url(raw_url)
            else:
                normalize_youtube_url(raw_url)
        except UrlValidationError as error:
            self._show_error("Invalid link", str(error))
            return
        self._set_busy(True, "Fetching playlist details…" if mode is DownloadMode.PLAYLIST else "Fetching video details…")
        self._reset_progress_display()
        self.title_label.setText("Reading playlist information…" if mode is DownloadMode.PLAYLIST else "Reading video information…")
        self.quality_combo.clear()
        self.info = None
        self.playlist_info = None
        self._playlist_selection.clear()
        self.playlist_table.setRowCount(0)
        self.subtitle_language_combo.clear()
        self._refresh_bitrate_labels()
        self._update_summary()
        self._update_download_button()

        if mode is DownloadMode.PLAYLIST:
            def operation(progress: Any, cancel_check: Any) -> PlaylistInfo:
                return self.engine.probe_playlist(raw_url, progress=progress, cancel_check=cancel_check)
        else:
            def operation(progress: Any, cancel_check: Any) -> VideoInfo:
                return self.engine.probe(raw_url, progress=progress, cancel_check=cancel_check)

        self.controller.start(operation)

    def _url_changed(self, _text: str) -> None:
        if self.controller.is_running:
            return
        self.info = None
        self.playlist_info = None
        self._playlist_selection.clear()
        self.playlist_table.setRowCount(0)
        self.quality_combo.clear()
        self.subtitle_language_combo.clear()
        self.title_label.setText("No link selected")
        self.status_label.setText("Fetch details to populate download options.")
        self._refresh_bitrate_labels()
        if self._selected_mode() is DownloadMode.QUEUE and self._selected_queue_media() is DownloadMode.VIDEO:
            # The batch height list is not tied to the single-video link, so it
            # has to be rebuilt rather than left empty.
            self._populate_queue_quality_choices()
        self._update_summary()
        self._update_download_button()

    def _combo_index_of(self, combo: QComboBox, value: object) -> int:
        """Find a row by comparing the stored values in Python.

        ``QComboBox.findData`` does not compare wrapped Python objects by
        equality, so two equal ``PlaylistQuality`` values are not matched and
        the lookup has to walk the rows itself.
        """

        for index in range(combo.count()):
            if combo.itemData(index) == value:
                return index
        return -1

    def _populate_queue_quality_choices(self) -> None:
        """Offer fixed heights for a batch that has not been probed yet.

        A queue can mix unrelated channels, so there is no shared list of real
        resolutions to show.  Each link resolves to the nearest available height
        at download time, and the top entry is effectively "whatever is best"
        because nothing sensible is higher than 2160p.
        """

        current = self.quality_combo.currentData()
        self.quality_combo.blockSignals(True)
        try:
            self.quality_combo.clear()
            for height in QUEUE_HEIGHT_CHOICES:
                quality = PlaylistQuality(height=height, fps=None)
                label = "Best (up to 2160p)" if height == QUEUE_HEIGHT_CHOICES[0] else f"{height}p"
                index = self.quality_combo.count()
                self.quality_combo.addItem(label, quality)
                self.quality_combo.setItemData(
                    index,
                    "Each link is saved at the highest height available up to this one",
                    Qt.ItemDataRole.ToolTipRole,
                )
            index = self._combo_index_of(self.quality_combo, current)
            if index < 0:
                # 1080p is the most common target, so it is the default.
                index = self._combo_index_of(
                    self.quality_combo, PlaylistQuality(height=1080, fps=None)
                )
            self.quality_combo.setCurrentIndex(max(0, index))
        finally:
            self.quality_combo.blockSignals(False)

    @staticmethod
    def _audio_size_estimate(duration: float | None, bitrate: int | None) -> int | None:
        return estimate_audio_size_bytes(duration, bitrate)

    def _populate_playlist_table(self, info: PlaylistInfo) -> None:
        """Fill the playlist list, keeping any selection the user already made."""

        self._updating_playlist_rows = True
        try:
            previous = dict(self._playlist_selection)
            self._playlist_selection = {}
            self.playlist_table.setRowCount(len(info.videos))
            for row, video in enumerate(info.videos):
                checkbox_item = QTableWidgetItem()
                checkbox_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                if not video.selectable:
                    checkbox_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    checkbox_item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    selected = previous.get(video.video_id, True)
                    self._playlist_selection[video.video_id] = selected
                    checkbox_item.setCheckState(
                        Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
                    )
                self.playlist_table.setItem(row, PLAYLIST_COLUMN_CHECK, checkbox_item)

                title_item = QTableWidgetItem(video.title or video.video_id)
                title_item.setToolTip(
                    f"{video.title or video.video_id}"
                    f"{'' if video.selectable else f' — {video.unavailable_reason}'}"
                )
                title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.playlist_table.setItem(row, PLAYLIST_COLUMN_TITLE, title_item)

                length_item = QTableWidgetItem(self._format_duration(video.duration) or "—")
                length_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                length_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.playlist_table.setItem(row, PLAYLIST_COLUMN_LENGTH, length_item)

                size_item = QTableWidgetItem()
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                size_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.playlist_table.setItem(row, PLAYLIST_COLUMN_SIZE, size_item)
        finally:
            self._updating_playlist_rows = False
        self._refresh_playlist_sizes()

    def _playlist_quality_label(self, info: PlaylistInfo, quality: PlaylistQuality) -> str:
        """Show availability next to a shared quality so fallback is explicit.

        The aggregate size is not useful here: a video that lacks the exact
        quality still gets the nearest one, so the per-row sizes carry the real
        numbers instead.
        """

        total = info.selectable_video_count
        if not total or quality.available_video_count >= total:
            return quality.resolution_label
        return f"{quality.resolution_label} · {quality.available_video_count}/{total} exact"

    def _queue_links(self) -> tuple[str, ...]:
        """Return the pasted links, normalized, de-duplicated, in order.

        The same video pasted twice is only downloaded once, and a URL that
        cannot be parsed is dropped here so one bad line does not abort the
        whole batch; the summary line reports how many were skipped.
        """

        links: list[str] = []
        seen: set[str] = set()
        for line in self.queue_edit.toPlainText().splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                normalized = normalize_youtube_url(candidate)
            except UrlValidationError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            links.append(normalized)
        return tuple(links)

    def _queue_input_counts(self) -> tuple[int, int, int]:
        """Return (accepted, duplicated, invalid) counts for the pasted lines."""

        accepted: set[str] = set()
        duplicates = invalid = 0
        for line in self.queue_edit.toPlainText().splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                normalized = normalize_youtube_url(candidate)
            except UrlValidationError:
                invalid += 1
                continue
            if normalized in accepted:
                duplicates += 1
                continue
            accepted.add(normalized)
        return len(accepted), duplicates, invalid

    def _selected_queue_media(self) -> DownloadMode:
        value = self.queue_media_combo.currentData()
        try:
            return DownloadMode(value)
        except ValueError:
            return DownloadMode.VIDEO

    def _queue_links_changed(self) -> None:
        if self._selected_mode() is not DownloadMode.QUEUE:
            return
        self._rebuild_queue_table()
        self._update_summary()
        self._update_download_button()

    def _queue_media_changed(self, _index: int = -1) -> None:
        """Switching what each link saves changes which per-link options apply."""

        if self._selected_mode() is not DownloadMode.QUEUE:
            return
        self.quality_combo.clear()
        if self._selected_queue_media() is DownloadMode.VIDEO:
            self._populate_queue_quality_choices()
        self._mode_changed()

    def _clear_queue(self) -> None:
        self.queue_edit.clear()
        self._rebuild_queue_table()
        self._update_summary()
        self._update_download_button()

    def _rebuild_queue_table(self) -> None:
        """Render one row per accepted link, keeping any live row state.

        Results are keyed by the normalized URL rather than the row index so
        that editing the text box does not shuffle a finished job's history.
        """

        finished = {
            self.queue_table.item(row, QUEUE_COLUMN_LINK).text(): (
                self.queue_table.item(row, QUEUE_COLUMN_STATUS).text(),
                self.queue_table.item(row, QUEUE_COLUMN_TITLE).text(),
                self.queue_table.item(row, QUEUE_COLUMN_SIZE).text(),
            )
            for row in range(self.queue_table.rowCount())
            if self.queue_table.item(row, QUEUE_COLUMN_LINK) is not None
            and self.queue_table.item(row, QUEUE_COLUMN_STATUS) is not None
        }
        links = self._queue_links()
        self.queue_table.setRowCount(len(links))
        for row, link in enumerate(links):
            status, title, size = finished.get(link, ("Queued", "", ""))
            for column, text, alignment in (
                (QUEUE_COLUMN_STATUS, status, Qt.AlignmentFlag.AlignCenter),
                (QUEUE_COLUMN_LINK, link, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                (QUEUE_COLUMN_TITLE, title, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                (QUEUE_COLUMN_SIZE, size, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            ):
                item = QTableWidgetItem(text)
                item.setTextAlignment(alignment)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == QUEUE_COLUMN_LINK:
                    item.setToolTip(link)
                self.queue_table.setItem(row, column, item)

        accepted, duplicates, invalid = self._queue_input_counts()
        if accepted == 0:
            self.queue_summary_label.setText("Add links above to build a queue.")
            return
        parts = [f"{accepted} link{'s' if accepted != 1 else ''} queued, one after another"]
        if duplicates:
            parts.append(f"{duplicates} duplicate{'s' if duplicates != 1 else ''} ignored")
        if invalid:
            parts.append(f"{invalid} line{'s' if invalid != 1 else ''} not a YouTube link")
        self.queue_summary_label.setText(" · ".join(parts))

    def _set_queue_row_status(self, url: str, status: str, title: str = "", size: str = "") -> bool:
        """Update the row showing ``url``; returns whether a row was found."""

        for row in range(self.queue_table.rowCount()):
            link_item = self.queue_table.item(row, QUEUE_COLUMN_LINK)
            if link_item is None or link_item.text() != url:
                continue
            if status:
                self.queue_table.setItem(row, QUEUE_COLUMN_STATUS, self._queue_plain_item(status))
            if title:
                self.queue_table.setItem(row, QUEUE_COLUMN_TITLE, self._queue_plain_item(title))
            if size:
                self.queue_table.setItem(row, QUEUE_COLUMN_SIZE, self._queue_plain_item(size))
            return True
        return False

    @staticmethod
    def _queue_plain_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return item

    def _selected_playlist_video_ids(self) -> tuple[str, ...]:
        if self.playlist_info is None:
            return ()
        return tuple(
            video.video_id
            for video in self.playlist_info.videos
            if self._playlist_selection.get(video.video_id, True) and video.selectable
        )

    def _playlist_row_video(self, row: int) -> VideoInfo | None:
        if self.playlist_info is None or not (0 <= row < len(self.playlist_info.videos)):
            return None
        return self.playlist_info.videos[row]

    def _set_all_playlist_rows(self, checked: bool) -> None:
        if self.playlist_info is None:
            return
        self._updating_playlist_rows = True
        try:
            for row, video in enumerate(self.playlist_info.videos):
                if not video.selectable:
                    continue
                item = self.playlist_table.item(row, PLAYLIST_COLUMN_CHECK)
                if item is None:
                    continue
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self._playlist_selection[video.video_id] = checked
        finally:
            self._updating_playlist_rows = False
        self._update_summary()
        self._update_download_button()

    def _playlist_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_playlist_rows or item.column() != PLAYLIST_COLUMN_CHECK:
            return
        video = self._playlist_row_video(item.row())
        if video is None:
            return
        self._playlist_selection[video.video_id] = item.checkState() == Qt.CheckState.Checked
        self._update_summary()
        self._update_download_button()

    def _refresh_playlist_sizes(self) -> None:
        """Show each video's own size for the currently selected quality."""

        if self.playlist_info is None:
            return
        media = self._selected_playlist_media()
        quality = self._selected_quality()
        bitrate = self.bitrate_combo.currentData()
        bitrate = bitrate if isinstance(bitrate, int) else None
        preference = self._selected_preference()
        qualifier = " (smaller file)" if preference is StreamPreference.SMALLER_FILE else ""
        self.playlist_table.setHorizontalHeaderLabels(
            ["Save", "Video", "Length", f"Size at {media.format_label}{qualifier}"]
        )
        for row, video in enumerate(self.playlist_info.videos):
            size_item = self.playlist_table.item(row, PLAYLIST_COLUMN_SIZE)
            if size_item is None:
                continue
            if not video.selectable:
                size_item.setText("unavailable")
                size_item.setToolTip(video.unavailable_reason or "This video cannot be downloaded.")
                continue
            if media is PlaylistMedia.AUDIO:
                size_bytes = playlist_audio_size_bytes(video, bitrate)
                note = "MP3 at the selected bitrate"
            else:
                if not isinstance(quality, PlaylistQuality):
                    size_bytes = None
                    note = "Choose a video quality to see sizes."
                else:
                    preference = self._selected_preference()
                    size_bytes = playlist_video_size_bytes(video, quality, preference)
                    selected_quality = select_playlist_video_quality(video, quality)
                    if selected_quality is None:
                        note = "No compatible quality for this video."
                    elif not quality_matches_target(selected_quality, quality):
                        note = f"Nearest available: {selected_quality.display_name}"
                    else:
                        note = f"Exact match: {selected_quality.display_name}"
            size_item.setText(f"~{format_size(size_bytes)}" if size_bytes else "size unavailable")
            size_item.setToolTip(note)

    def _refresh_bitrate_labels(self) -> None:
        duration = self.info.duration if self.info is not None else None
        for index in range(self.bitrate_combo.count()):
            bitrate = self.bitrate_combo.itemData(index)
            if not isinstance(bitrate, int):
                continue
            estimate = self._audio_size_estimate(duration, bitrate)
            suffix = f" · ~{format_size(estimate)}" if estimate is not None else ""
            self.bitrate_combo.setItemText(index, f"{bitrate} kbps{suffix}")

    @staticmethod
    def _why_no_smaller_file(quality: object) -> str:
        """Explain, truthfully, why "smaller file" is unavailable here.

        The message this replaces said the alternatives "are WebM, and converting
        those would mean re-encoding".  That was wrong in every case measured.
        Sprite Fright's 858p alternative is format 400, `ext=mp4`, `av01`,
        104 MB; at 128p it is 394, `ext=mp4`, `av01`, 4 MB.  Both are MP4 files.
        They need no re-encode and they are not WebM - the only thing against
        them is that this app offers H.264, so a user was told a falsehood about
        the very stream they were being offered instead.

        So the reason names what was passed up and what it would have saved.  If
        nothing smaller exists at all, it says only that, which is also true.

        An unrecognised codec omits the word rather than interpolating it.  The
        first draft fell back to "another codec" and read "YouTube has a smaller
        another codec stream here" - a grammar bug, caught by a test written to
        guard exactly this function.
        """

        unoffered = getattr(quality, "unoffered_smaller_size_bytes", None)
        codec = getattr(quality, "unoffered_smaller_codec", None)
        if not unoffered or unoffered <= 0:
            return (
                "YouTube offers only one H.264 stream at this resolution, so there "
                "is nothing smaller to switch to."
            )
        codec_name = {
            "vp9": "VP9",
            "av1": "AV1",
            "h264": "H.264",
        }.get(str(codec or "").lower())
        subject = f"a smaller {codec_name} stream" if codec_name else "a smaller stream"
        return (
            f"YouTube has {subject} here, about {format_size(unoffered)}, but this "
            f"app only offers H.264, so it is not offered either."
        )

    def _sync_preference_availability(self) -> None:
        """Offer "smaller file" only where a leaner H.264 stream really exists.

        YouTube publishes a single H.264 stream at some resolutions, and at 1440p
        and 2160p the smaller streams it does publish are usually VP9 or AV1,
        which this app does not offer.  Offering the choice there would quietly
        hand back the same file, so the entry is disabled and says why instead of
        pretending to save something.

        Those refused streams are frequently MP4 containers, not WebM, and they
        need no re-encode - see `_why_no_smaller_file`, which exists because the
        reason given here used to claim otherwise and was wrong.

        A batch queue is exempt: no link has been probed yet, and each one
        resolves its own preference when it is reached.
        """

        index = self._combo_index_of(
            self.stream_preference_combo, StreamPreference.SMALLER_FILE.value
        )
        item = self.stream_preference_combo.model().item(index) if index >= 0 else None
        if item is None:
            return
        if self._selected_mode() is DownloadMode.QUEUE:
            available, reason = True, StreamPreference.SMALLER_FILE.tradeoff
        else:
            quality = self.quality_combo.currentData()
            # A single video answers this outright; a playlist answers it with
            # a count of how many of its videos have an alternative.
            alternative = getattr(quality, "has_smaller_alternative", None)
            if alternative is not None:
                available = bool(alternative)
            else:
                count = getattr(quality, "smaller_video_count", None)
                available = isinstance(count, int) and count > 0
            reason = (
                StreamPreference.SMALLER_FILE.tradeoff
                if available
                else self._why_no_smaller_file(quality)
            )
        item.setEnabled(available)
        item.setToolTip(reason)
        self.stream_preference_combo.setToolTip(
            f"Best quality: {StreamPreference.QUALITY.tradeoff}\n"
            f"Smaller file: {reason}\n\n"
            "Smaller file never re-encodes, and it offers only H.264 in an MP4 "
            "container, so a smaller VP9 or AV1 stream is passed up even when its "
            "bitrate is lower."
        )
        if not available and self._selected_preference() is StreamPreference.SMALLER_FILE:
            # Falling back quietly would keep a stale choice in the request, so
            # move the selection rather than leaving an option that cannot apply.
            self.stream_preference_combo.blockSignals(True)
            try:
                self.stream_preference_combo.setCurrentIndex(
                    self._combo_index_of(
                        self.stream_preference_combo, StreamPreference.QUALITY.value
                    )
                )
            finally:
                self.stream_preference_combo.blockSignals(False)

    def _update_summary(self, _index: int = -1) -> None:
        # Called on every quality, preference, mode, and details change, which
        # is exactly when the smaller-stream offer can appear or disappear.
        self._sync_preference_availability()
        self._render_quality_labels()
        mode = self._selected_mode()
        if mode is DownloadMode.PLAYLIST:
            self._update_playlist_summary()
            return
        if mode is DownloadMode.QUEUE:
            self._update_queue_summary()
            return
        if mode is DownloadMode.SUBTITLES:
            self._set_summary(self._subtitle_summary_text(self._selected_subtitle_format()))
        elif self.info is None:
            # Nothing has been read yet, so the lists have no entries to show.
            self._set_summary("Fetch video details to see the available sizes")
        else:
            # Video, audio, and thumbnail sizes are all in their own lists now.
            self._set_summary("")

    def _set_summary(self, text: str) -> None:
        """Show a summary line, or hide the row when there is nothing to say."""

        self.summary_label.setText(text)
        self.summary_label.setVisible(bool(text))

    def _render_quality_labels(self) -> None:
        """Cost each quality list entry against the selected stream preference.

        The list is now the only place a video size is shown, so it has to
        follow the preference: choosing "smaller file" must change what every
        row says, not leave the best-quality number in place.  Entries that
        have no size to show are left as they were.
        """

        preference = self._selected_preference()
        for index in range(self.quality_combo.count()):
            quality = self.quality_combo.itemData(index)
            if isinstance(quality, VideoQuality):
                label = (
                    f"{quality.resolution_label} · "
                    f"{quality.size_label_for_preference(preference)}"
                )
                self.quality_combo.setItemText(index, label)

    def _subtitle_summary_text(self, subtitle_format: SubtitleFormat) -> str:
        """Describe the caption file(s) a subtitles run will write."""

        extension = subtitle_format.extension
        if self.info is None:
            return "Captions: fetch video details to see which languages are available"
        if not self.info.subtitles_available:
            return "Captions: YouTube advertises none for this video"
        if self._selected_subtitle_source() is SubtitleSource.ALL:
            count = len(self._all_subtitle_languages())
            noun = "file" if count == 1 else "files"
            advertised = len(self._advertised_subtitle_languages())
            if advertised > count:
                return (
                    f"Captions: {count} {extension} {noun}, one per track "
                    f"({advertised} advertised, capped at {MAX_SUBTITLE_LANGUAGES})"
                )
            return f"Captions: {count} {extension} {noun}, one per advertised track"
        return f"Captions: one {extension} file"

    def _advertised_subtitle_languages(self) -> tuple[str, ...]:
        """Every track YouTube advertises, author-written and automatic."""

        if self.info is None:
            return ()
        return tuple(
            dict.fromkeys(track.language_code for track in self.info.subtitle_tracks)
        )

    def _all_subtitle_languages(self) -> tuple[str, ...]:
        """The capped set ``ALL`` will actually write.

        Mirrors ``Engine._subtitle_language_options`` so the count shown before
        a download is the number of files that will appear afterwards.
        """

        return tuple(sorted(self._advertised_subtitle_languages())[:MAX_SUBTITLE_LANGUAGES])

    def _selected_subtitle_language(self, mode: DownloadMode) -> str:
        """The one language to request, or empty to let the source decide.

        ``ALL`` must send nothing so the engine applies its own capped list
        rather than being handed an uncapped "everything" request.
        """

        if mode is not DownloadMode.SUBTITLES:
            return ""
        if self._selected_subtitle_source() is SubtitleSource.ALL:
            return ""
        value = self.subtitle_language_combo.currentData()
        return str(value) if value else ""

    def _subtitle_languages_for_display(self) -> tuple[str, ...]:
        """Language codes a subtitles run would write for the current source.

        This mirrors ``Engine._subtitle_language_options`` so the UI reports
        exactly what the job will attempt, including the author-first fallback.
        """

        if self.info is None:
            return ()
        source = self._selected_subtitle_source()
        if source is SubtitleSource.ALL:
            return self._all_subtitle_languages()
        automatic = tuple(
            sorted(
                {
                    track.language_code
                    for track in self.info.automatic_subtitle_tracks
                }
            )
        )[:MAX_SUBTITLE_LANGUAGES]
        if source is SubtitleSource.AUTOMATIC:
            return automatic
        manual = tuple(
            dict.fromkeys(track.language_code for track in self.info.manual_subtitle_tracks)
        )
        if manual:
            return manual
        return automatic

    def _populate_subtitle_languages(self) -> None:
        """Fill the language combo from the tracks the current source allows."""

        current = self.subtitle_language_combo.currentData()
        self.subtitle_language_combo.blockSignals(True)
        try:
            self.subtitle_language_combo.clear()
            if self._selected_subtitle_source() is SubtitleSource.ALL:
                count = len(self._all_subtitle_languages())
                self.subtitle_language_combo.addItem(f"All advertised tracks ({count})", "")
                self.subtitle_language_combo.setToolTip(
                    SubtitleSource.ALL.help_text
                )
                return
            languages = self._subtitle_languages_for_display()
            if not languages:
                self.subtitle_language_combo.addItem("No captions advertised", "")
                return
            names = self._subtitle_language_names(languages)
            for code in languages:
                self.subtitle_language_combo.addItem(names.get(code, code), code)
            index = self.subtitle_language_combo.findData(current)
            if index >= 0:
                self.subtitle_language_combo.setCurrentIndex(index)
        finally:
            self.subtitle_language_combo.blockSignals(False)

    def _subtitle_language_names(self, languages: tuple[str, ...]) -> dict[str, str]:
        """Map a language code to the best display name yt-dlp advertised."""

        names: dict[str, str] = {}
        for track in self.info.subtitle_tracks if self.info is not None else ():
            if track.language_code not in languages:
                continue
            existing = names.get(track.language_code)
            # A manual track is a better label than its auto-generated twin.
            if existing is None or (not track.automatic and existing.endswith("(auto)")):
                suffix = " (auto)" if track.automatic else ""
                names[track.language_code] = f"{track.language_name}{suffix}"
        return names

    def _update_queue_summary(self) -> None:
        """Queue mode is not probed up front, so only the shape is described."""

        links = self._queue_links()
        if not links:
            self._set_summary("Add at least one link to the queue")
            return
        media = self._selected_queue_media()
        noun = QUEUE_MEDIA_LABELS[media].split(" (")[0].lower()
        plural = "" if len(links) == 1 else "s"
        if media is DownloadMode.VIDEO:
            quality = self.quality_combo.currentText() or "the chosen quality"
            self._set_summary(
                f"Queued {len(links)} {noun} link{plural}: each link is saved at the "
                f"nearest available {quality}."
            )
        elif media is DownloadMode.AUDIO:
            self._set_summary(
                f"Queued {len(links)} {noun} link{plural}: each link becomes one "
                f"{QUEUE_MEDIA_LABELS[media]}."
            )
        else:
            self._set_summary(
                f"Queued {len(links)} {noun} link{plural}: each link is read first, "
                "so a failure does not stop the rest."
            )

    def _update_playlist_summary(self) -> None:
        if self.playlist_info is None:
            self._set_summary("")
            self.playlist_summary_label.setText("Fetch a playlist to choose videos.")
            return

        selected_ids = set(self._selected_playlist_video_ids())
        selected = [video for video in self.playlist_info.videos if video.video_id in selected_ids]
        total = len(self.playlist_info.videos)
        if not selected:
            self._set_summary("")
            self.playlist_summary_label.setText("No videos selected. Tick a video to include it.")
            return

        media = self._selected_playlist_media()
        bitrate_value = self.bitrate_combo.currentData()
        bitrate = bitrate_value if isinstance(bitrate_value, int) else None
        quality = self._selected_quality()

        # Per-video sizes are shown in the table's own column, so this only
        # reports the things a column of numbers cannot: how many rows will
        # land on a nearby quality instead of the exact one.
        missing = 0
        fallback = 0
        for video in selected:
            if media is PlaylistMedia.AUDIO:
                if not playlist_audio_size_bytes(video, bitrate):
                    missing += 1
            elif isinstance(quality, PlaylistQuality):
                if not playlist_video_size_bytes(video, quality, self._selected_preference()):
                    missing += 1
                selected_quality = select_playlist_video_quality(video, quality)
                if selected_quality is not None and not quality_matches_target(selected_quality, quality):
                    fallback += 1

        parts: list[str] = []
        if missing:
            parts.append(
                f"size unavailable for {missing} "
                f"{'video' if missing == 1 else 'videos'}"
            )
        if not isinstance(quality, PlaylistQuality) and media is not PlaylistMedia.AUDIO:
            parts.insert(0, "Select a video quality to size the rows")
        self._set_summary("; ".join(parts))

        summary_parts = [f"{len(selected)} of {total} videos selected"]
        if fallback:
            summary_parts.append(f"{fallback} will use a nearby quality")
        unavailable = self.playlist_info.unavailable_video_count
        if unavailable:
            noun = "entry" if unavailable == 1 else "entries"
            summary_parts.append(f"{unavailable} {noun} unavailable and not downloadable")
        if self.playlist_info.skipped_count:
            skipped = self.playlist_info.skipped_count
            noun = "entry" if skipped == 1 else "entries"
            summary_parts.append(f"{skipped} {noun} skipped while reading")
        self.playlist_summary_label.setText(" · ".join(summary_parts))

    def _mode_changed(self) -> None:
        mode = self._selected_mode()
        is_playlist = mode is DownloadMode.PLAYLIST
        is_queue = mode is DownloadMode.QUEUE
        self.playlist_group.setVisible(is_playlist)
        self.queue_group.setVisible(is_queue)
        self.playlist_media_label.setVisible(is_playlist)
        self.playlist_media_combo.setVisible(is_playlist)
        # A batch takes its links from the queue box and reads each one itself,
        # so the single-video URL field and its Fetch button would only mislead.
        busy = self.controller.is_running
        self.url_edit.setEnabled(not busy and not is_queue)
        self.fetch_button.setEnabled(not busy and not is_queue)
        media = self._selected_playlist_media()
        if is_playlist:
            show_quality = media is PlaylistMedia.VIDEO
            show_bitrate = media is PlaylistMedia.AUDIO
            self.quality_label.setText("Video quality for all selected videos")
        elif is_queue:
            queue_media = self._selected_queue_media()
            show_quality = queue_media is DownloadMode.VIDEO
            show_bitrate = queue_media is DownloadMode.AUDIO
            self.quality_label.setText("Video quality for every link")
        else:
            show_quality = mode is DownloadMode.VIDEO
            show_bitrate = mode is DownloadMode.AUDIO
            self.quality_label.setText("Video quality")
        self.quality_label.setVisible(show_quality)
        self.quality_combo.setVisible(show_quality)
        self.bitrate_label.setVisible(show_bitrate)
        self.bitrate_combo.setVisible(show_bitrate)
        # Cover art only exists inside an MP3, so the switch rides on the very
        # condition that reveals the bitrate picker rather than asking the user
        # to notice that it is irrelevant in video and subtitle modes.
        self.embed_cover_checkbox.setVisible(show_bitrate)
        # The stream preference changes which format each link is saved as, so
        # it is shown wherever a video quality can be chosen.
        show_preference = show_quality
        self.stream_preference_label.setVisible(show_preference)
        self.stream_preference_combo.setVisible(show_preference)
        self._sync_subtitle_visibility(is_playlist, media, mode)
        if is_playlist:
            self._refresh_playlist_sizes()
        if is_queue:
            self._rebuild_queue_table()
            if not self.quality_combo.count():
                self._populate_queue_quality_choices()
        self._refresh_retry_button()
        self._update_summary()
        self._update_download_button()

    def _sync_subtitle_visibility(
        self,
        is_playlist: bool,
        media: PlaylistMedia,
        mode: DownloadMode,
    ) -> None:
        """Show the caption controls for the modes that can write subtitle files.

        Queue mode downloads each link as one of the single-video media kinds,
        so it needs the same controls as those modes.
        """

        if is_playlist:
            visible = media is PlaylistMedia.SUBTITLES
        else:
            visible = mode is DownloadMode.SUBTITLES or mode is DownloadMode.QUEUE
        for widget in (
            self.subtitle_format_label,
            self.subtitle_format_combo,
            self.subtitle_source_label,
            self.subtitle_source_combo,
            self.subtitle_language_label,
            self.subtitle_language_combo,
        ):
            widget.setVisible(visible)
        if visible and not is_playlist:
            self.subtitle_language_combo.setVisible(mode is DownloadMode.SUBTITLES)

    def _playlist_media_changed(self, _index: int = -1) -> None:
        self._mode_changed()

    def _selected_mode(self) -> DownloadMode:
        value = self.mode_combo.currentData()
        try:
            return DownloadMode(value)
        except ValueError:
            return DownloadMode.VIDEO

    def _selected_playlist_media(self) -> PlaylistMedia:
        value = self.playlist_media_combo.currentData()
        try:
            return PlaylistMedia(value)
        except ValueError:
            return PlaylistMedia.VIDEO

    def _selected_preference(self) -> StreamPreference:
        value = self.stream_preference_combo.currentData()
        try:
            return StreamPreference(value)
        except ValueError:
            return StreamPreference.QUALITY

    def _selected_subtitle_format(self) -> SubtitleFormat:
        value = self.subtitle_format_combo.currentData()
        try:
            return SubtitleFormat(value)
        except ValueError:
            return SubtitleFormat.SRT

    def _selected_subtitle_source(self) -> SubtitleSource:
        value = self.subtitle_source_combo.currentData()
        try:
            return SubtitleSource(value)
        except ValueError:
            return SubtitleSource.PREFERRED

    def _preference_changed(self, _index: int = -1) -> None:
        """Re-cost every size under the new stream preference."""

        self._update_summary()
        self._refresh_playlist_sizes()
        if self.playlist_info is not None:
            self._refresh_playlist_sizes()
        self._update_download_button()

    def _subtitle_source_changed(self, _index: int = -1) -> None:
        source = self._selected_subtitle_source()
        self.subtitle_source_combo.setToolTip(source.help_text)
        self._populate_subtitle_languages()
        self._update_summary()
        self._update_download_button()

    def _selected_quality(self) -> VideoQuality | PlaylistQuality | None:
        value = self.quality_combo.currentData()
        if isinstance(value, (VideoQuality, PlaylistQuality)):
            return value
        return None

    def _update_download_button(self) -> None:
        mode = self._selected_mode()
        if mode is DownloadMode.PLAYLIST:
            media = self._selected_playlist_media()
            if self.playlist_info is None or not self._selected_playlist_video_ids():
                valid = False
            elif media is PlaylistMedia.AUDIO:
                valid = self.bitrate_combo.currentData() in {128, 192, 256, 320}
            elif media is PlaylistMedia.SUBTITLES:
                valid = True
            else:
                valid = isinstance(self._selected_quality(), PlaylistQuality)
        elif mode is DownloadMode.QUEUE:
            valid = bool(self._queue_links())
            if valid and self._selected_queue_media() is DownloadMode.VIDEO:
                valid = self.quality_combo.currentData() is not None
        else:
            if self.info is None:
                self.download_button.setEnabled(False)
                return
            valid = True
            if mode is DownloadMode.VIDEO:
                valid = isinstance(self._selected_quality(), VideoQuality) and self.info.audio_available
            elif mode is DownloadMode.AUDIO:
                valid = self.info.audio_available and self.bitrate_combo.currentData() in {128, 192, 256, 320}
            elif mode is DownloadMode.THUMBNAIL:
                valid = bool(self.info.thumbnail_url)
            elif mode is DownloadMode.SUBTITLES:
                valid = self.info.subtitles_available
        self.download_button.setEnabled(valid and not self.controller.is_running)

    def _start_download(self) -> None:
        if self.controller.is_running:
            return
        mode = self._selected_mode()
        output_text = self.destination_edit.text().strip()
        if not output_text:
            self._show_error("Destination required", "Choose a folder for the downloaded file.")
            return
        if mode is DownloadMode.PLAYLIST:
            playlist_info = self.playlist_info
            if playlist_info is None:
                return
            media = self._selected_playlist_media()
            bitrate_value = self.bitrate_combo.currentData()
            bitrate = bitrate_value if isinstance(bitrate_value, int) else None
            quality = self._selected_quality() if media is PlaylistMedia.VIDEO else None
            if media is PlaylistMedia.VIDEO and not isinstance(quality, PlaylistQuality):
                return
            selected_ids = self._selected_playlist_video_ids()
            if not selected_ids:
                self._show_error("No videos selected", "Select at least one playlist video to download.")
                return
            request = PlaylistDownloadRequest(
                url=playlist_info.normalized_url or self.url_edit.text().strip(),
                output_dir=Path(output_text),
                info=playlist_info,
                quality=quality,
                media=media,
                audio_bitrate=bitrate if media is PlaylistMedia.AUDIO else None,
                selected_video_ids=selected_ids,
                preference=self._selected_preference(),
                subtitle_format=self._selected_subtitle_format(),
                subtitle_source=self._selected_subtitle_source(),
                embed_cover=self.embed_cover_checkbox.isChecked(),
            )
        elif mode is DownloadMode.QUEUE:
            links = self._queue_links()
            if not links:
                self._show_error(
                    "No links in the queue",
                    "Paste at least one YouTube link, one per line.",
                )
                return
            queue_media = self._selected_queue_media()
            quality = self._selected_quality() if queue_media is DownloadMode.VIDEO else None
            if queue_media is DownloadMode.VIDEO and not isinstance(quality, PlaylistQuality):
                return
            bitrate_value = self.bitrate_combo.currentData()
            request = QueueDownloadRequest(
                urls=links,
                output_dir=Path(output_text),
                media=queue_media,
                quality=quality,
                audio_bitrate=bitrate_value if queue_media is DownloadMode.AUDIO else None,
                preference=self._selected_preference(),
                subtitle_format=self._selected_subtitle_format(),
                subtitle_source=self._selected_subtitle_source(),
                embed_cover=self.embed_cover_checkbox.isChecked(),
            )
            self._reset_queue_rows()
        else:
            if self.info is None:
                return
            quality = self._selected_quality() if mode is DownloadMode.VIDEO else None
            bitrate = self.bitrate_combo.currentData() if mode is DownloadMode.AUDIO else None
            request = DownloadRequest(
                url=self.info.normalized_url or self.url_edit.text().strip(),
                output_dir=Path(output_text),
                mode=mode,
                info=self.info,
                quality=quality,
                audio_bitrate=bitrate,
                preference=self._selected_preference(),
                subtitle_format=self._selected_subtitle_format(),
                subtitle_source=self._selected_subtitle_source(),
                subtitle_language=self._selected_subtitle_language(mode),
                embed_cover=self.embed_cover_checkbox.isChecked(),
            )
        if mode is DownloadMode.PLAYLIST:
            message = "Starting playlist download…"
        elif mode is DownloadMode.QUEUE:
            message = f"Starting batch of {len(request.urls)} links…"
        else:
            message = "Starting download…"

        def operation(progress: Any, cancel_check: Any) -> Any:
            if mode is DownloadMode.PLAYLIST:
                return self.engine.download_playlist(request, progress=progress, cancel_check=cancel_check)
            return self.engine.download(request, progress=progress, cancel_check=cancel_check)

        self._start_download_job(request, message, operation)

    def _reset_queue_rows(self) -> None:
        """Mark every queued row as waiting again before a new batch starts."""

        for row in range(self.queue_table.rowCount()):
            if self.queue_table.item(row, QUEUE_COLUMN_STATUS) is not None:
                self.queue_table.setItem(row, QUEUE_COLUMN_STATUS, self._queue_plain_item("Waiting"))
            if self.queue_table.item(row, QUEUE_COLUMN_SIZE) is not None:
                self.queue_table.setItem(row, QUEUE_COLUMN_SIZE, self._queue_plain_item(""))

    def _cancel_job(self) -> None:
        if self.controller.is_running:
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling…")
            self.controller.cancel()

    def _start_download_job(self, request: Any, message: str, operation: Any) -> None:
        """Run a download job, remembering just enough to offer a Resume.

        Every download path goes through here rather than starting the worker
        itself, because Pause is only meaningful for a job that can be
        continued, and only this method knows which request is running and what
        the destination held before it began.  A probe or an update check starts
        a worker without passing through here, so its `_pauseable` stays False
        and the Pause button stays dead for it.
        """

        self._pauseable = True
        self._pausing = False
        self._active_request = request
        self._active_operation = operation
        self._paused_request = None
        # Taken before the first byte rather than read afterwards: the pieces
        # this job adds are the only evidence of which leftovers it owns, and
        # there is no second chance to look at the folder as it was.
        self._resume_snapshot = {
            item.target_name: _piece_fingerprint(item)
            for item in self._unfinished_in(request.output_dir)
        }
        self._set_busy(True, message)
        self._reset_progress_display()
        self.controller.start(operation)

    @staticmethod
    def _unfinished_in(output_dir: Path) -> tuple[UnfinishedDownload, ...]:
        """Interrupted downloads in ``output_dir``, never raising.

        A destination may not exist yet, may be a file, or may be unreadable;
        none of those is a reason to refuse to start a download.
        """

        try:
            return unfinished_downloads(output_dir)
        except OSError:
            return ()

    def _resumable_downloads(self, output_dir: Path) -> tuple[UnfinishedDownload, ...]:
        """Interrupted downloads *this job* is responsible for and can continue.

        Three conditions, and dropping any one of them makes the button lie:
        real bytes have to be on disk (a lone state file is a run that started,
        not one that got anywhere), the piece has to differ from what the folder
        held before the job began (or an earlier session's crash is offered as
        though it were this one), and the job has to have been stopped by Pause
        rather than finished or Cancelled.
        """

        if self._paused_request is None:
            return ()
        return tuple(
            item
            for item in self._unfinished_in(output_dir)
            if item.has_fragments
            and self._resume_snapshot.get(item.target_name) != _piece_fingerprint(item)
        )

    def _pause_job(self) -> None:
        """Stop the running download gently, keeping every piece written so far.

        This is a cancel that intends to continue, and the distinction is the
        whole of what the button means.  yt-dlp has no pause primitive to call,
        so the stop is the same stop Cancel performs -- which is also why the
        partial file can still be locked for as long as the worker lives.  What
        the app can promise is that nothing is deleted and that Resume is
        offered afterwards if there is anything real to continue; that the next
        run then continues at the byte it reached rather than starting over is
        **not** established, and no wording here claims otherwise.
        """

        if not self.controller.is_running or not self._pauseable:
            return
        self._pausing = True
        self._paused_request = self._active_request
        self.pause_button.setEnabled(False)
        self.status_label.setText("Stopping - what has downloaded so far is kept")
        self.status_bar.showMessage("Stopping - what has downloaded so far is kept", 8000)
        self.controller.cancel()

    def _resume_paused_job(self) -> None:
        """Re-run the exact job Pause stopped.

        The stored request is used rather than the settings now on screen, for
        the same reason Resume is offered at all: the request is the only record
        of how this download was being made.  It also keeps the file going to
        the folder it started in, whatever the destination box has since been
        changed to.

        The snapshot is deliberately **not** retaken.  Those pieces are what the
        continuation is going to write into, and re-sampling here would file
        them as pre-existing -- so a second pause would then look like it
        produced nothing and Resume would vanish.
        """

        request = self._paused_request
        operation = self._active_operation
        if request is None or operation is None or self.controller.is_running:
            return
        self._paused_request = None
        self._pausing = False
        self._pauseable = True
        self._set_busy(True, "Continuing the download that was paused…")
        self._reset_progress_display()
        self.controller.start(operation)

    def _refresh_resume_button(self) -> None:
        """Show Resume only when it can actually do something.

        Hidden rather than left greyed out, which is stricter than Retry and is
        the point of the control: it appears when a stopped download is really
        there to continue and is not there at all otherwise.
        """

        request = self._paused_request
        if request is None or self.controller.is_running:
            self.resume_button.setEnabled(False)
            self.resume_button.setVisible(False)
            return
        resumable = self._resumable_downloads(request.output_dir)
        if not resumable:
            self.resume_button.setEnabled(False)
            self.resume_button.setVisible(False)
            return
        count = len(resumable)
        noun = "download" if count == 1 else "downloads"
        self.resume_button.setVisible(True)
        self.resume_button.setEnabled(True)
        self.resume_button.setToolTip(
            f"Continue the {count} {noun} the Pause stopped; they keep saving to the "
            "folder they started in, and what has already arrived is kept"
        )

    def _reset_progress_display(self, *, indeterminate: bool = True) -> None:
        """Start the bar again for a new operation.

        This has to run at the start of an operation rather than being left to
        the previous one's completion.  Reading a video's details ends at 100%,
        and the download that follows starts from nothing, so that 100% has to
        be discarded here.  It stays discarded because `JobController` drops a
        superseded worker's events, so nothing can re-assert it afterwards.
        """

        self._progress_value = -1
        self.status_label.setToolTip("")
        if indeterminate:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)

    def _on_progress(self, event: Any) -> None:
        percent = getattr(event, "percent", None)
        message = getattr(event, "message", "Working…")
        speed = getattr(event, "speed", None)
        eta = getattr(event, "eta", None)
        if percent is None:
            if self.progress_bar.minimum() != 0 or self.progress_bar.maximum() != 0:
                self.progress_bar.setRange(0, 0)
        else:
            if self.progress_bar.minimum() != 0 or self.progress_bar.maximum() != 100:
                self.progress_bar.setRange(0, 100)
            try:
                value = max(0, min(100, int(percent)))
            except (TypeError, ValueError):
                value = 0
            # A retried fragment can report a lower byte count.  Keeping the
            # displayed value monotonic prevents visible backwards jumps.
            #
            # This clamp is safe against a *previous operation's* value only
            # because `JobController` never relays one: a worker that has been
            # superseded is dropped at the source, so every event reaching here
            # belongs to the operation `_reset_progress_display` last cleared
            # for.  Without that, one leftover 100% from a finished probe would
            # clamp every value of the download that followed it and pin the
            # bar at 100% for the whole run.
            value = max(value, self._progress_value)
            if value != self.progress_bar.value():
                self.progress_bar.setValue(value)
            self._progress_value = value
        details = [message]
        if speed:
            details.append(speed)
        if eta:
            details.append(eta)
        self.status_label.setText(" · ".join(details))

    def _on_job_thread_finished(self) -> None:
        if not self.controller.is_running:
            self._set_busy(False)

    def _on_job_succeeded(self, result: Any) -> None:
        if isinstance(result, VideoInfo):
            self.playlist_info = None
            self.info = result
            duration = self._format_duration(result.duration)
            live_marker = " · live" if result.is_live else ""
            self.title_label.setText(f"{result.title} · {duration}{live_marker}" if duration else result.title)
            self.quality_combo.clear()
            for quality in result.qualities:
                self.quality_combo.addItem(quality.resolution_label, quality)
            self._render_quality_labels()
            self._refresh_bitrate_labels()
            self._populate_subtitle_languages()
            self._update_summary()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._progress_value = 100
            self._set_busy(False, "Video details ready — choose a download mode")
            return

        if isinstance(result, PlaylistInfo):
            self.info = None
            self.playlist_info = result
            skipped = f" · {result.skipped_count} skipped" if result.skipped_count else ""
            unavailable = (
                f" · {result.unavailable_video_count} unavailable"
                if result.unavailable_video_count
                else ""
            )
            self.title_label.setText(
                f"{result.title} · {result.video_count} videos{unavailable}{skipped}"
            )
            self.quality_combo.clear()
            for quality in result.qualities:
                self.quality_combo.addItem(self._playlist_quality_label(result, quality), quality)
            self._populate_playlist_table(result)
            self._refresh_bitrate_labels()
            self._update_summary()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._progress_value = 100
            self._set_busy(False, "Playlist details ready — choose videos, media, and one quality")
            return

        if isinstance(result, PlaylistDownloadResult):
            self._set_busy(False, "Playlist download complete")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._progress_value = 100
            self._remember_result_paths(result.paths)
            # The engine records the failed video ids rather than their titles,
            # so a playlist with a repeated title still retries the right video.
            self._last_failed_playlist = tuple(result.failed_video_ids)
            self._refresh_retry_button()
            noun = result.item_label
            summary = f"Playlist complete: {result.success_count} of {result.total} {noun}s saved as {result.media.format_label}"
            first_failure = ""
            if result.failed_count:
                summary += f"; {result.failed_count} failed and skipped"
                first_failure = result.failures[0][1] if result.failures else ""
                if first_failure:
                    summary += f"; first failure: {first_failure}"
            if result.fallback_video_count:
                summary += f"; {result.fallback_video_count} used a nearby quality"
            self.status_label.setText(summary)
            self.status_label.setToolTip(first_failure)
            self.status_bar.showMessage(summary, 8000)
            self._update_download_button()
            return

        if isinstance(result, QueueDownloadResult):
            self._set_busy(False, "Batch download complete")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._progress_value = 100
            self._apply_queue_results(result)
            return

        # Claimed before the DownloadResult branch below, which would otherwise
        # treat a finished check as a finished download and report "Download
        # complete" with no file to show.
        if isinstance(result, UpdateCheck):
            automatic = self._update_check_automatic
            self._update_check_automatic = False
            self._set_busy(False)
            if not automatic:
                # The manual check put the bar into its indeterminate state at
                # the start and has to put it back.  An automatic one never
                # touched it, and filling it here would be the program
                # reporting progress on a job that was invisible by design.
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(100)
                self._progress_value = 100
            self._report_update(result, automatic=automatic)
            return

        self._set_busy(False, "Download complete")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._progress_value = 100
        path = getattr(result, "path", None)
        if path is not None:
            # subtitle_paths already includes path, so a caption job must not
            # list it twice.
            captions = tuple(getattr(result, "subtitle_paths", ())) or (path,)
            self._remember_result_paths(captions if result.mode is DownloadMode.SUBTITLES else (path,))
            if result.mode is DownloadMode.SUBTITLES and len(captions) > 1:
                self.status_label.setText(
                    f"Saved {len(captions)} caption files to: {path.parent}"
                )
            else:
                self.status_label.setText(f"Saved to: {path}")
            # A run YouTube cut short still saved real files, so the reason it
            # stopped is shown next to them rather than replacing them.
            warning = getattr(result, "warning", "")
            if warning:
                self.status_label.setText(f"{self.status_label.text()} · {warning}")
                self.status_label.setToolTip(warning)
                self.status_bar.showMessage(warning, 10000)
            else:
                self.status_bar.showMessage("Download complete", 5000)
        self._update_download_button()

    def _apply_queue_results(self, result: QueueDownloadResult) -> None:
        """Write per-link outcomes into the queue table and report a summary."""

        for item in result.items:
            if item.path is not None:
                status = "Saved"
                try:
                    size = f"~{format_size(item.path.stat().st_size)}"
                except OSError:
                    size = ""
            else:
                status = "Failed"
                size = item.error
            self._set_queue_row_status(item.url, status, item.title or "—", size)

        saved = result.saved
        # Store failed links for retry.  A batch already knows the whole link for each
        # failed row, so no lookup back into the queue text is needed.
        self._last_failed_queue = tuple(item.url for item in result.failures)
        self._refresh_retry_button()
        self._remember_result_paths(tuple(item.path for item in saved if item.path is not None))
        summary = f"Batch complete: {len(saved)} of {result.total} links saved"
        failures = result.failures
        if failures:
            summary += f"; {len(failures)} failed and skipped"
        if result.fallback_count:
            summary += f"; {result.fallback_count} used a nearby quality"
        self.status_label.setText(summary)
        self.status_label.setToolTip(failures[0].error if failures else "")
        self.status_bar.showMessage(summary, 8000)
        self._update_download_button()

    def _remember_result_paths(self, paths: tuple[Path, ...]) -> None:
        """Keep the files a job produced so Open folder / Play can act on them."""

        existing = tuple(path for path in paths if path.is_file())
        if not existing:
            # Fall back to the folder so "Open folder" still works.
            folders = {path.parent for path in paths}
            self._last_result_paths = ()
            self._last_result_folder = next(iter(folders), None)
            self._refresh_result_actions()
            return
        self._last_result_paths = existing
        self._last_result_folder = existing[0].parent
        self._refresh_result_actions()

    def _refresh_result_actions(self) -> None:
        playable = [
            path
            for path in self._last_result_paths
            if path.suffix.lower() not in SUBTITLE_EXTENSIONS and path.suffix.lower() not in IMAGE_EXTENSIONS
        ]
        self.open_folder_button.setEnabled(bool(self._last_result_folder))
        self.play_button.setEnabled(bool(playable))
        if playable:
            self.play_button.setToolTip(f"Open in the default player: {playable[0].name}")
        else:
            self.play_button.setToolTip("Open the downloaded file in the default player")

    def _open_last_folder(self) -> None:
        folder = self._last_result_folder
        if folder is None or not folder.is_dir():
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _play_last_file(self) -> None:
        playable = [
            path
            for path in self._last_result_paths
            if path.suffix.lower() not in SUBTITLE_EXTENSIONS and path.suffix.lower() not in IMAGE_EXTENSIONS
        ]
        if not playable:
            return
        # A batch may have saved many files; the first one is the sensible
        # default because it is the first link the user listed.
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(playable[0])))

    def _refresh_retry_button(self) -> None:
        """Name the Retry button after what it would actually retry.

        The resting label is a plain "Retry", which says what the control does
        without promising a rescue that may not be available.  Once a playlist
        or batch job has left failures behind it becomes "Retry failed" and the
        tooltip counts them, so the button explains itself before it is pressed.
        A single-video mode has no list to retry and the button stays off.
        """

        mode = self._selected_mode()
        if mode is DownloadMode.PLAYLIST:
            count = len(self._last_failed_playlist)
            noun = "video" if count == 1 else "videos"
        elif mode is DownloadMode.QUEUE:
            count = len(self._last_failed_queue)
            noun = "link" if count == 1 else "links"
        else:
            count, noun = 0, ""
        if count:
            self.retry_button.setText("Retry failed")
            self.retry_button.setToolTip(
                f"Try the {count} {noun} the last job could not save again"
            )
        else:
            self.retry_button.setText("Retry")
            self.retry_button.setToolTip(
                "Run the last playlist or batch download again; nothing is waiting to be retried"
            )
        self.retry_button.setEnabled(count > 0 and not self.controller.is_running)

    def _retry_failed_items(self) -> None:
        """Re-download only the items that failed in the last playlist/queue job.

        The retry uses the settings currently shown in the window, restricted to
        the items the previous job could not save.  Playlist retries are matched
        on the video ids the engine recorded, so a title that appears twice in a
        playlist cannot send the wrong video.
        """
        if self.controller.is_running:
            return

        # The retry button only exists to rescue one of these two jobs, so which
        # list it reads is decided by the mode rather than by the mode the last
        # job happened to use.
        mode = self._selected_mode()
        output_text = self.destination_edit.text().strip()
        if not output_text:
            self._show_error("Destination required", "Choose a folder for the downloaded file.")
            return
        if mode is DownloadMode.PLAYLIST:
            failed_ids = self._last_failed_playlist
            if self.playlist_info is None:
                self._show_error("Nothing to retry", "Fetch the playlist again before retrying.")
                return
            media = self._selected_playlist_media()
            bitrate_value = self.bitrate_combo.currentData()
            bitrate = bitrate_value if isinstance(bitrate_value, int) else None
            quality = self._selected_quality() if media is PlaylistMedia.VIDEO else None
            if media is PlaylistMedia.VIDEO and not isinstance(quality, PlaylistQuality):
                return
            if not failed_ids:
                self._show_error("Nothing to retry", "The last playlist download had no failed videos.")
                return
            # The playlist is re-probed from the same page, but the selection is
            # narrowed to the videos that failed, so nothing is downloaded twice.
            request = PlaylistDownloadRequest(
                url=self.playlist_info.normalized_url or self.url_edit.text().strip(),
                output_dir=Path(output_text),
                info=self.playlist_info,
                quality=quality,
                media=media,
                audio_bitrate=bitrate if media is PlaylistMedia.AUDIO else None,
                selected_video_ids=tuple(failed_ids),
                preference=self._selected_preference(),
                subtitle_format=self._selected_subtitle_format(),
                subtitle_source=self._selected_subtitle_source(),
                embed_cover=self.embed_cover_checkbox.isChecked(),
            )
            message = f"Retrying {len(failed_ids)} failed playlist video{'s' if len(failed_ids) != 1 else ''}"
        elif mode is DownloadMode.QUEUE:
            failed_urls = self._last_failed_queue
            if not failed_urls:
                self._show_error("Nothing to retry", "The last batch download had no failed links.")
                return
            queue_media = self._selected_queue_media()
            quality = self._selected_quality() if queue_media is DownloadMode.VIDEO else None
            if queue_media is DownloadMode.VIDEO and not isinstance(quality, PlaylistQuality):
                return
            bitrate_value = self.bitrate_combo.currentData()
            request = QueueDownloadRequest(
                urls=tuple(failed_urls),
                output_dir=Path(output_text),
                media=queue_media,
                quality=quality,
                audio_bitrate=bitrate_value if queue_media is DownloadMode.AUDIO else None,
                preference=self._selected_preference(),
                subtitle_format=self._selected_subtitle_format(),
                subtitle_source=self._selected_subtitle_source(),
                embed_cover=self.embed_cover_checkbox.isChecked(),
            )
            message = f"Retrying {len(failed_urls)} failed link{'s' if len(failed_urls) != 1 else ''}"
        else:
            self._show_error(
                "Nothing to retry",
                "Retry works after a playlist or batch download; single videos have no list to retry.",
            )
            return

        def operation(progress: Any, cancel_check: Any) -> Any:
            if mode is DownloadMode.PLAYLIST:
                return self.engine.download_playlist(request, progress=progress, cancel_check=cancel_check)
            return self.engine.download_queue(request, progress=progress, cancel_check=cancel_check)

        self._start_download_job(request, message, operation)

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return ""
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _on_job_failed(self, error: Any) -> None:
        # Consumed first, so a failure cannot leave the *next* check - which
        # the person asked for from the Help menu - reporting as though the
        # program had raised its hand on its own.
        automatic = self._update_check_automatic
        self._update_check_automatic = False
        if automatic:
            # `check_for_updates` documents that it never raises and turns
            # every failure into a result, so this should be unreachable.  It
            # is handled anyway because the promise of an automatic check is
            # that it cannot interrupt anybody, and an unattended modal dialog
            # is precisely that.
            _LOGGER.info("automatic_update_check_failed category=%s", type(error).__name__)
            self._set_busy(False)
            return
        self._set_busy(False, "Operation failed")
        self.progress_bar.setRange(0, 100)
        if isinstance(error, AppError):
            self._show_error("Operation failed", error.message)
        else:
            self._show_error("Operation failed", "An unexpected error stopped the operation.")

    def _on_job_cancelled(self) -> None:
        # A stop that was asked for by Pause still ends as a cancellation as far
        # as the worker is concerned; only the wording differs, and only here.
        self._update_check_automatic = False
        paused = self._pausing
        self._pausing = False
        if paused:
            self._set_busy(False, "Paused - what had downloaded so far is kept")
        else:
            self._set_busy(False, "Cancelled")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._progress_value = 0
        self._update_download_button()

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        is_queue = self._selected_mode() is DownloadMode.QUEUE
        self.fetch_button.setEnabled(not busy)
        self.url_edit.setEnabled(not busy and not is_queue)
        self.mode_combo.setEnabled(not busy)
        self.playlist_media_combo.setEnabled(not busy)
        self.quality_combo.setEnabled(
            not busy and (is_queue or self.info is not None or self.playlist_info is not None)
        )
        self.bitrate_combo.setEnabled(not busy)
        self.embed_cover_checkbox.setEnabled(not busy)
        self.stream_preference_combo.setEnabled(not busy)
        self.subtitle_format_combo.setEnabled(not busy)
        self.subtitle_source_combo.setEnabled(not busy)
        self.subtitle_language_combo.setEnabled(not busy)
        self.playlist_table.setEnabled(not busy)
        self.select_all_button.setEnabled(not busy and self.playlist_info is not None)
        self.select_none_button.setEnabled(not busy and self.playlist_info is not None)
        self.queue_edit.setEnabled(not busy)
        self.queue_media_combo.setEnabled(not busy)
        self.queue_clear_button.setEnabled(not busy)
        self.queue_table.setEnabled(not busy)
        self.destination_edit.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        # Pause is only offered for a download job: there is nothing to pause in
        # reading a video's details or checking for an update, and a Pause that
        # stopped one of those would simply be a second Cancel.
        self.pause_button.setEnabled(busy and self._pauseable)
        # Read last: they derive their enabled state from is_running.
        self._refresh_retry_button()
        self._refresh_resume_button()
        if not busy:
            # Every job ends here rather than at its own handler, so clearing
            # this is what keeps a later probe from being pausable because the
            # download before it was.
            self._pauseable = False
        if not busy:
            self._refresh_result_actions()
        if message:
            self.status_label.setText(message)
            self.status_bar.showMessage(message)
        if not busy:
            self._mode_changed()
            self._update_download_button()

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event: Any) -> None:
        if self.controller.is_running:
            answer = QMessageBox.question(
                self,
                "Download in progress",
                "A background operation is still running. Cancel it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.controller.cancel()
            stopped = self.controller.wait(10_000)
            if stopped:
                # Deliver the queued QThread.finished signal before the
                # window/controller can be destroyed.
                QCoreApplication.processEvents()
            if not stopped or self.controller.is_running:
                QMessageBox.warning(
                    self,
                    "Still stopping",
                    "The background operation has not stopped yet. Please wait a moment and try closing again.",
                )
                event.ignore()
                return
        # Only once the close is actually going ahead: a refused close leaves
        # the window up, and the check should still be waiting for it.
        self._update_check_timer.stop()
        if self._update_toast is not None:
            self._update_toast.hide()
        event.accept()
