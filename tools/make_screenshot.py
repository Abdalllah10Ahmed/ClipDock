"""Render the real ClipDock windows to PNGs for the README.

A repository for a desktop application with no picture of it is the most
conspicuous thing missing: the repo page is a wall of text, and GitHub shows a
screenshot in search results. This produces those pictures from the real
widgets rather than from a mock-up, so they cannot drift away from the program
the way a hand-drawn diagram does.

What it does
    Builds the actual MainWindow, puts it into the state the requested view is
    meant to show, runs the real details fetch, waits for it, and saves the
    window as a PNG. Four views, because one picture of one mode undersells a
    program that does playlists, batches and audio:

        --view video      the default single-video flow (default output
                          docs/screenshot.png)
        --view playlist   playlist mode, against a real 18-video playlist
        --view queue      batch queue mode, with real links in the box
        --view first-run  the dependency dialog, the one that appears before
                          the window does

    --theme is orthogonal: --view video --theme Dark is a fifth picture for the
    price of a flag.

Four deliberate constraints
    **It never touches the real settings file.** The window reads and writes
    %LOCALAPPDATA%\\ClipDock\\settings.json for the theme, so running this with
    a --theme would otherwise silently change the theme on the machine that ran
    it. The path is redirected to a temporary file for the life of the process.

    **It renders offscreen by default**, so it never flashes a window or steals
    focus while you are doing something else. Pass --native to render on the
    real desktop instead, which is slower and briefly visible.

    **It sizes the window to its content.** The window opens at 860x700, which
    is not tall enough for the interface: the row of buttons along the bottom
    hangs below the fold and a scrollbar appears. That is what the program
    really looks like on launch, but it is a poor README image, so the tool
    grows the window until nothing is scrolled out of view. It asks the
    scrollbars how much is hidden rather than guessing a size, so it keeps
    working if a row is ever added.

    **It refuses to write an image of nothing.** See "What the check is for"
    below, which is the part worth knowing before changing anything here.

    **It stages the first-run dialog, and says so here rather than only in a
    comment.** The dependency dialog only ever appears on a machine with no
    FFmpeg, and this one has one - deliberately put on PATH by hand to test the
    device-wide detection. Rendering it truthfully would produce a dialog that
    announces it found FFmpeg and offers to download FFmpeg in the same breath.
    So --view first-run patches the device lookup to report nothing found, and
    renders the real dialog with a real missing-dependency report. Everything
    else in that image is live: the free-space figure, the install folder, the
    wording. What is staged is the *answer* to the search, not the dialog.

    Every other view is unstaged. The video and playlist details are fetched
    from YouTube during the run, and the queue links are real ones that resolve.

Run as:  .\\.venv\\Scripts\\python.exe tools\\make_screenshot.py

What the check is for, precisely
    Nothing that writes this file can look at the result, so the image is
    verified three ways. It is worth being exact about what each one proves,
    because the obvious version of this check is worthless and was written and
    then measured:

    * An earlier version sampled the image on a 48x48 grid and rejected fewer
      than 40 distinct colours. That threshold is meaningless here. An unfetched
      window - no title, no quality list, the program's own "fetch details"
      prompt - measures 81 grey levels, 7.9% ink and a tone deviation of 32.5,
      against 108, 10.0% and 39.6 for a correct render. It passed, and it was
      right to: that is a real, correctly painted window showing an empty state.
      This window is densely covered in labels, borders and controls, so there is
      no arrangement of "is anything drawn" statistics that separates the useful
      screenshot from the useless one. They are nearly the same picture.

    So the checks are split by what each can actually establish:

    1. Content, verified from the window's own state rather than from pixels.
       This is the one that matters, because it is the only one that can tell a
       populated window from an empty one. It asks whether the fetch produced a
       real result with real formats in it. Worded against widget state, so it
       cannot be satisfied by a window that happens to be full of text.

    2. Fonts, verified by geometry. Windows' offscreen platform plugin starts
       with an empty font database - 0 families here against 163 native - and
       every character then renders as a box. The first screenshot this tool
       produced was exactly that: a correctly laid out window holding real video
       details and nine real quality options, entirely unreadable. It passed
       tier 1 because the content was genuinely real, and it passed tier 3
       because a wall of boxes has plenty of tonal variety. Neither could see
       it, which is why the font database is now checked on its own.

    3. Image sanity, verified arithmetically. Weak on its own, but it does catch
       the cases where the grab itself went wrong: a null pixmap, an all-one
       colour buffer, a zero-sized image, a stylesheet that never applied and
       left the window unstyled. The thresholds are loose because it is only
       ever meant to catch a broken grab, not to judge how the program looks.

    4. Fit, verified against the scrollbars. A clipped screenshot is worse than
       no screenshot, and the first two versions of this file produced one: the
       footer buttons were below the fold. Nothing about that is visible in the
       pixels - a scrollbar looks like a scrollbar - so it is checked by asking
       whether anything is still hidden, which is the one question the window
       itself can answer.

    Neither check can confirm the image is *good*. Nothing here has been looked
    at. Whoever runs this should open the PNG before committing it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

# Must be set before PySide6 is imported anywhere, or the platform plugin has
# already been chosen and this has no effect.  setdefault, not assignment, so
# --native can override it through the environment.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from youtube_downloader.core import settings as settings_module  # noqa: E402
from youtube_downloader.core.dependencies import DependencyReport  # noqa: E402
from youtube_downloader.core.logging_setup import configure_logging  # noqa: E402
from youtube_downloader.core.models import DownloadMode  # noqa: E402
from youtube_downloader.gui import dependencies as dependencies_module  # noqa: E402
from youtube_downloader.gui.dependencies import DependencyDialog  # noqa: E402
from youtube_downloader.gui.main_window import (  # noqa: E402
    PLAYLIST_COLUMN_SIZE,
    PLAYLIST_COLUMN_TITLE,
    QUEUE_COLUMN_LINK,
    MainWindow,
)
from youtube_downloader.gui.themes import DEFAULT_THEME, THEMES  # noqa: E402

# Big Buck Bunny, from the Blender Foundation: a real video that is genuinely
# free to feature in a project's own screenshot, and one whose format list is
# long enough that the quality dropdown fills properly.
DEFAULT_VIDEO = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"

# The Blender Foundation's own "Blender Open Movies" playlist, from its official
# channel. Chosen by asking the channel for its playlists rather than by
# recalling an ID from memory, which is how a stale or wrong one would get in.
# Eighteen entries is enough for the table to look like a real batch without
# being slow enough to time out.
DEFAULT_PLAYLIST = "https://www.youtube.com/playlist?list=PLav47HAVZMjnTFVZL-aImCQIC0uLZtNCz"

# Four real Blender open movies, taken from that playlist so every one of them
# is known to resolve. The queue builds its rows by parsing this text, so a
# typo here shows up as a missing row rather than as a broken link later.
DEFAULT_QUEUE_LINKS = (
    "https://www.youtube.com/watch?v=_cMxraX_5RE",  # Sprite Fright
    "https://www.youtube.com/watch?v=39h7ZJbRcsw",  # OVERGROWN
    "https://www.youtube.com/watch?v=l5OZu-IrXpw",  # SINGULARITY
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",  # Big Buck Bunny
)

VIEW_VIDEO = "video"
VIEW_PLAYLIST = "playlist"
VIEW_QUEUE = "queue"
VIEW_FIRST_RUN = "first-run"
VIEWS = (VIEW_VIDEO, VIEW_PLAYLIST, VIEW_QUEUE, VIEW_FIRST_RUN)

# Which views need YouTube to be up before the image means anything.
VIEWS_THAT_FETCH = frozenset({VIEW_VIDEO, VIEW_PLAYLIST})

# Output name per view, so a run cannot quietly overwrite the main screenshot
# with the playlist one. The suffix is empty for the default view, which keeps
# docs/screenshot.png where the README already points.
VIEW_SUFFIXES = {
    VIEW_VIDEO: "",
    VIEW_PLAYLIST: "-playlist",
    VIEW_QUEUE: "-queue",
    VIEW_FIRST_RUN: "-first-run",
}

# The window's own first-run size, used as the *starting* size rather than the
# final one. The window opens at 860x700 and its content does not fit there -
# the footer buttons hang below the fold behind a scrollbar - so fit_to_content
# grows it until the whole interface is visible. Starting from the real
# first-run size rather than an invented one keeps the width, spacing and
# control sizes in the picture identical to what a user sees on launch.
DEFAULT_SIZE = (860, 700)

# Titles the window shows while it has nothing real to display.  These mirror
# inline literals in main_window.py rather than constants there; the test pins
# them against that source so a wording change breaks the test loudly instead
# of quietly letting an empty-state render be called a screenshot.
#
# The trailing ellipsis is written as an escape on purpose.  It was first typed
# here as a literal character, copied out of PowerShell 5.1 output, which decodes
# files as Windows-1252 and turns U+2026 into three characters - so the constant
# held mojibake and silently stopped matching the window.  An escape cannot be
# mangled by whatever shell reads this file.
NON_CONTENT_TITLES = frozenset(
    {
        "No link selected",
        "Reading playlist information\u2026",
        "Reading video information\u2026",
    }
)

# Floors for the image-sanity tier only.  Deliberately loose: a real render
# measures far above all of them, and their job is to catch a broken grab.  See
# the module docstring for the measurement this was calibrated against.
MIN_GREY_LEVELS = 40
MIN_TONE_STDEV = 6.0
MIN_INK_FRACTION = 0.01

# The window sets no font family of its own, so whatever Qt picks as the default
# is what a user sees. Naming the font explicitly makes the screenshot
# reproducible: the same image on a machine that prefers Tahoma.
PREFERRED_UI_FONTS = ("Segoe UI", "Tahoma", "Calibri", "Arial", "Verdana")

# A real font is far wider on 'W' than on 'i'.  A glyph-less fallback draws the
# same box for every character, so the two measure identically and this ratio
# collapses to exactly 1.0.  Measured: 1.000 with no fonts, 3.50 for the default
# and 3.75 for Segoe UI.  The floor sits between the two with room to spare.
MIN_GLYPH_WIDTH_RATIO = 2.0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--view",
        choices=VIEWS,
        default=VIEW_VIDEO,
        help="which part of the program to photograph (default: video)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="PNG path to write (default: docs/screenshot<suffix>.png for the view)",
    )
    parser.add_argument("--video", default=DEFAULT_VIDEO, help="YouTube URL for --view video")
    parser.add_argument("--playlist", default=DEFAULT_PLAYLIST, help="playlist URL for --view playlist")
    parser.add_argument("--theme", default=None, help="theme label to render, e.g. Dark")
    parser.add_argument(
        "--width", type=int, default=DEFAULT_SIZE[0], help="starting width; grows if needed"
    )
    parser.add_argument(
        "--height", type=int, default=DEFAULT_SIZE[1], help="starting height; grows if needed"
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the fetch")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the details fetch and render an empty window. Only useful for "
        "checking that the content check below rejects it, which it must.",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="render on the real desktop instead of offscreen",
    )
    return parser.parse_args()


def configure_offscreen_fonts() -> str | None:
    """Point the offscreen platform at the system font directory.

    Windows' offscreen platform plugin starts with an *empty* font database.
    Measured on this machine: 0 font families offscreen against 163 native. With
    no fonts at all, Qt falls back to a glyph-less font and every character in
    the window renders as a box - a screenshot that looks busy and structurally
    correct and is completely unreadable, which is exactly what the first one
    this tool produced was. QT_QPA_FONTDIR repairs it, restoring 67 families
    including Segoe UI.

    Two details make this a function rather than a line at module scope.

    It must run before the QApplication is *constructed*, because that is when
    the plugin populates the database; loading fonts afterwards does not rebuild
    it. It does not have to precede the PySide6 import - verified, the platform
    plugin has not read the variable by then.

    And it deliberately does not happen merely because this module was imported.
    The test suite imports it, and a font database appearing as a side effect of
    that would change how test_gui renders, which has quietly been drawing boxes
    and has tests asserting on painted glyphs. Silently improving an unrelated
    test module's environment is not a gift. So the caller's first job is to
    ask for it.

    Returns the directory it pointed at, or None when it did not apply.
    """

    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return None
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not fonts.is_dir():
        return None
    os.environ.setdefault("QT_QPA_FONTDIR", str(fonts))
    return os.environ["QT_QPA_FONTDIR"]


def pick_theme(requested: str | None) -> str | None:
    """Resolve a theme label to its id, or None to use whatever the window picks.

    Matching is on the human label, because that is what a person reads in the
    window's own selector.  A typo is an error rather than a silent fallback:
    a screenshot rendered in the wrong theme is worse than no screenshot.
    """

    if requested is None:
        return None
    for theme_id, label, _description in THEMES:
        if label.lower() == requested.lower() or theme_id.lower() == requested.lower():
            return theme_id
    available = ", ".join(label for _id, label, _d in THEMES)
    raise SystemExit(f"unknown theme {requested!r}. available: {available}")


def wait_for_fetch(window: MainWindow, app: QApplication, timeout: float) -> str:
    """Spin the event loop until the details fetch settles.

    Returns "ok", "failed" or "timeout".  The controller keeps is_running true
    until its queued teardown slot has run, so waiting on that rather than on
    the worker's own signal is what makes this deterministic.
    """

    outcome = {"result": "timeout"}
    window.controller.succeeded.connect(lambda _r: outcome.update(result="ok"))
    window.controller.failed.connect(lambda _e: outcome.update(result="failed"))
    window.controller.cancelled.connect(lambda: outcome.update(result="failed"))

    deadline = time.monotonic() + timeout
    while window.controller.is_running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    # Let the window's own result-handling slot run before anything is read or
    # painted; processEvents is what delivers it.
    for _ in range(10):
        app.processEvents()
    return outcome["result"]


def apply_ui_font(app: QApplication) -> str | None:
    """Name a real UI font so the render does not depend on Qt's default choice.

    Returns the family that was applied, or None when nothing suitable was
    found - which is not itself a failure, because check_fonts below is what
    decides whether the text can be read.
    """

    families = set(QFontDatabase.families())
    for name in PREFERRED_UI_FONTS:
        if name in families:
            app.setFont(QFont(name))
            return name
    return None


def glyph_width_ratio(font: QFont) -> float:
    """How much wider 'W' is than 'i'. Exactly 1.0 means every glyph is a box.

    This is the check that catches unreadable text. A glyph-less fallback draws
    one identical rectangle for every character, so narrow and wide measure the
    same; a real font does not. It is a crude proxy for "the font has letterforms"
    and it was chosen because the obvious alternatives do not work here:
    QRawFont.supportsCharacter reports False for the middle dot, the ellipsis and
    the multiplication sign even in Arial and Calibri, which all contain them, and
    a pixel-histogram test scored 0.96 for tofu against 0.98 for real text. Only
    this one separates the two cases cleanly.
    """

    metrics = QFontMetrics(font)
    narrow = metrics.horizontalAdvance("i" * 20)
    wide = metrics.horizontalAdvance("W" * 20)
    return wide / narrow if narrow else 0.0


def check_fonts(app: QApplication) -> tuple[str, float, list[str]]:
    """Check that the text in the window can actually be read.

    A third tier, added after the first screenshot came out as a wall of boxes.
    It is worth being clear that neither existing tier could have caught that:
    the window held real video info with nine real quality options, so the
    content check passed, and a screen full of tofu boxes has plenty of tonal
    variety, so the image check passed too. Both reported a good screenshot of
    something nobody can read.
    """

    problems: list[str] = []
    families = QFontDatabase.families()
    if not families:
        return "", 0.0, [
            "Qt sees no fonts at all, so every character would render as a box. "
            "QT_QPA_FONTDIR should point at the Windows font directory; if this "
            "persists, re-run with --native."
        ]

    ratio = glyph_width_ratio(app.font())
    if ratio < MIN_GLYPH_WIDTH_RATIO:
        problems.append(
            f"glyph width ratio {ratio:.2f} (want >= {MIN_GLYPH_WIDTH_RATIO}): the font has no "
            f"letterforms, so the text would render as identical boxes"
        )
    return app.font().family(), ratio, problems


def fit_to_content(
    window: MainWindow,
    app: QApplication,
    requested: tuple[int, int],
    ceiling: tuple[int, int] = (1400, 1600),
) -> tuple[tuple[int, int], list[str]]:
    """Grow the window until nothing is scrolled out of view.

    The window opens at 860x700 and its content does not fit there: the root
    widget lives inside a QScrollArea, so the footer row of buttons hangs below
    the fold and a scrollbar sits in the picture. That is honest - it is what
    the program looks like on first launch - but it makes a poor README image,
    because the reader cannot see the whole interface at once.

    Sizing to the content instead of to a magic number also means this keeps
    working when the window grows a row. The loop asks the scrollbars for their
    hidden range rather than guessing at the content's height, because the
    scrollbar range is the ground truth about what is not being shown. Growing
    width at the same time matters: removing a vertical scrollbar widens the
    viewport, which can pull a horizontal scrollbar back in.

    Returns the fitted size and any problems. A non-empty problem list means the
    content is taller than the ceiling allows and the image would still be
    clipped, which is worth failing on rather than committing.
    """

    width, height = requested
    scroll_area = getattr(window, "_scroll_area", None)
    if scroll_area is None:
        return (width, height), []

    for _attempt in range(12):
        window.resize(width, height)
        app.processEvents()
        hidden_vertically = scroll_area.verticalScrollBar().maximum()
        hidden_horizontally = scroll_area.horizontalScrollBar().maximum()
        if hidden_vertically == 0 and hidden_horizontally == 0:
            return (width, height), []

        width = min(width + max(hidden_horizontally, 16), ceiling[0])
        height = min(height + max(hidden_vertically, 16), ceiling[1])
        if (width, height) >= (ceiling[0], ceiling[1]):
            break

    # One more resize at the ceiling so the reported numbers describe what
    # would actually be captured rather than the last size tried.
    window.resize(ceiling[0], ceiling[1])
    app.processEvents()
    still_hidden = scroll_area.verticalScrollBar().maximum()
    return (ceiling[0], ceiling[1]), [
        f"the window content still does not fit: {still_hidden}px of it is scrolled out of "
        f"view even at {ceiling[0]}x{ceiling[1]}, so the screenshot would be clipped"
    ]


def select_mode(window: MainWindow, mode: DownloadMode) -> None:
    """Switch the window's mode dropdown, the way a person clicking it would.

    By index rather than by item text, because the item data is the mode value
    and a wording change to the label should not be able to break a screenshot.
    """

    for index in range(window.mode_combo.count()):
        if window.mode_combo.itemData(index) == mode.value:
            window.mode_combo.setCurrentIndex(index)
            return
    raise SystemExit(f"the window offers no {mode.value!r} mode to photograph")


def apply_view(window: MainWindow, view: str, arguments: argparse.Namespace) -> None:
    """Put the window into the state this view is meant to show.

    Every step here goes through the same public signal a click would, so the
    window's own visibility and summary logic runs exactly as it does for a
    person. Setting a widget's text directly and hoping the layout follows is
    how a screenshot ends up showing two modes at once.
    """

    if view == VIEW_PLAYLIST:
        select_mode(window, DownloadMode.PLAYLIST)
        window.url_edit.setText(arguments.playlist)
    elif view == VIEW_QUEUE:
        select_mode(window, DownloadMode.QUEUE)
        # textChanged is what a user typing fires, and it is what builds the rows.
        window.queue_edit.setPlainText("\n".join(DEFAULT_QUEUE_LINKS))
    else:
        # The single-video and subtitle views both read the one URL box. This
        # branch was missing when the view support was first written, and its
        # absence was invisible: an empty URL is a validation error, and a
        # validation error opens a modal message box, and a modal message box
        # with nobody to click OK hangs the process rather than failing. See
        # errors_become_reports below.
        window.url_edit.setText(arguments.video)


def build_first_run_dialog() -> DependencyDialog:
    """Build the real dependency dialog, in the state it appears on a fresh PC.

    Staged in exactly one respect, which is described in the module docstring:
    the device lookup is told the computer has no FFmpeg. Without that the
    dialog would say it found one and offer to download one in the same
    paragraph, because this machine does have one.

    Everything else is live. free_space_bytes() reads the actual volume,
    install_targets() the actual folder, and describe() formats the real report
    through the real function. Nothing here draws or words anything itself.
    """

    original = dependencies_module.locate_device_ffmpeg
    dependencies_module.locate_device_ffmpeg = lambda: None
    try:
        report = DependencyReport(
            name="FFmpeg",
            present=False,
            path=None,
            needed_for="merging an audio and video track into one file, and "
            "converting thumbnails into thumbnails",
            download_bytes=164 * 1_048_576,
            installable=True,
        )
        return DependencyDialog([report])
    finally:
        dependencies_module.locate_device_ffmpeg = original


def check_video_content(window: MainWindow) -> list[str]:
    """Report whether the window holds something worth putting in a README.

    This is the check that carries the weight, because it is the only one that
    can distinguish a populated window from an empty one. It reads the window's
    own state rather than its pixels, which is what makes it work: an empty
    window is full of text and passes any image statistic ever devised.
    """

    problems: list[str] = []
    info = getattr(window, "info", None)
    if info is None:
        problems.append("the fetch produced no video info, so the window is empty")
    else:
        qualities = getattr(info, "qualities", ())
        if not qualities:
            problems.append("the video info carries no quality options")

    shown = window.quality_combo.count()
    if shown < 1:
        problems.append("the quality dropdown is empty, so nothing was selectable")

    title = window.title_label.text().strip()
    if not title:
        problems.append("the title label is blank")
    elif title in NON_CONTENT_TITLES:
        problems.append(f"the title is the empty-state placeholder {title!r}")

    return problems


def check_playlist_content(window: MainWindow) -> list[str]:
    """The playlist check asks about the table, because the table is the feature.

    A populated playlist and an empty one differ in nothing a pixel statistic
    could see: same labels, same dropdowns, same columns, no rows. The row count
    and the text in the rows are the only thing that tells them apart.
    """

    problems: list[str] = []
    rows = window.playlist_table.rowCount()
    if rows < 2:
        problems.append(f"the playlist table holds {rows} row(s), so it is not showing a playlist")
        return problems

    titled = sum(
        1
        for row in range(rows)
        if (item := window.playlist_table.item(row, PLAYLIST_COLUMN_TITLE)) is not None
        and item.text().strip()
    )
    if titled == 0:
        problems.append("every playlist row is blank, so the titles never arrived")
    elif titled < rows // 2:
        problems.append(f"only {titled} of {rows} playlist rows have a title")

    # Reported rather than required. The size column is what makes the playlist
    # view worth a screenshot, so a run that lost every size is worth seeing in
    # the output - but an empty size cell is a legitimate state for a video with
    # no compatible format, so it is not a reason to refuse the image.
    sized = sum(
        1
        for row in range(rows)
        if (item := window.playlist_table.item(row, PLAYLIST_COLUMN_SIZE)) is not None
        and item.text().strip()
    )
    if sized == 0:
        print(f"  note    : no playlist row shows a size, so that column is empty in the image")
    else:
        print(f"  note    : {sized} of {rows} playlist rows show a size")
    return problems


def check_queue_content(window: MainWindow) -> list[str]:
    """The queue check asks how many links survived parsing.

    An unparseable link is dropped silently by design - a person pasting a
    mixture of links and notes should not be blocked by a typo - so a queue that
    silently lost three of four rows would render as a perfectly respectable,
    nearly empty table.
    """

    problems: list[str] = []
    expected = len(DEFAULT_QUEUE_LINKS)
    rows = window.queue_table.rowCount()
    if rows != expected:
        problems.append(
            f"the queue table holds {rows} row(s) for {expected} link(s), so some did not parse"
        )
    blank = sum(
        1
        for row in range(rows)
        if (item := window.queue_table.item(row, QUEUE_COLUMN_LINK)) is None or not item.text().strip()
    )
    if blank:
        problems.append(f"{blank} queue row(s) show no link")
    return problems


def check_dialog_content(dialog: DependencyDialog) -> list[str]:
    """The dialog has no fetched data, so what is checked is that it says something.

    A dialog with an empty detail label would still be a correctly laid out,
    correctly themed, completely empty rectangle - which is precisely the shape
    of screenshot that got shipped three times in this tool's own history.
    """

    problems: list[str] = []
    detail = dialog.detail_label.text().strip()
    if len(detail) < 80:
        problems.append(f"the dialog explains {len(detail)} characters, which is not an explanation")
    if "FFmpeg" not in detail:
        problems.append("the dialog does not name what it is asking about")
    if not dialog.install_button.text().strip() or not dialog.later_button.text().strip():
        problems.append("a button has no label, so the dialog offers no choice")
    return problems


def check_content(subject: object, view: str) -> list[str]:
    """Dispatch the content check to whichever question this view can answer."""

    if view == VIEW_PLAYLIST:
        return check_playlist_content(subject)
    if view == VIEW_QUEUE:
        return check_queue_content(subject)
    if view == VIEW_FIRST_RUN:
        return check_dialog_content(subject)
    return check_video_content(subject)


def measure(image: QImage) -> dict[str, float]:
    """Summarise a render numerically.

    Three numbers, all over every pixel of the image:

        levels  distinct grey values. A solid fill has exactly one. Antialiased
                text alone puts a real render in the hundreds.
        stdev   spread of the tone. Blank is 0 by definition, so this catches a
                render that drew only one colour.
        ink     share of pixels that differ substantially from the dominant
                value, i.e. how much of the image is content rather than fill.

    The histogram runs over the full-resolution image, not a sample grid. An
    earlier grid-based version sampled 48x48 points and reported 13 distinct
    colours for a perfectly good render, because a window that is mostly
    background has few colours *at the sampled points*. The grid was measuring
    the sample, not the image. Counting every pixel through a 256-bucket
    histogram is C-speed anyway, so there was never a reason to sample.
    """

    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    # One byte per pixel in this format, so the length is known outright.  It is
    # not read from the binding: this PySide6 build does not expose byteCount(),
    # and depending on a version-specific accessor for arithmetic this simple is
    # not worth it.
    byte_count = gray.width() * gray.height()
    try:
        data = bytes(gray.constBits().asstring(byte_count))
    except AttributeError:
        data = bytes(gray.constBits())[:byte_count]
    if not data:
        return {"levels": 0, "stdev": 0.0, "ink": 0.0}

    counts = Counter(data)
    total = len(data)
    mean = sum(value * n for value, n in counts.items()) / total
    variance = sum(n * (value - mean) ** 2 for value, n in counts.items()) / total
    dominant = max(counts, key=lambda value: counts[value])
    ink = sum(n for value, n in counts.items() if abs(value - dominant) > 24) / total
    return {"levels": len(counts), "stdev": variance**0.5, "ink": ink}


def check_image(image: QImage, expected: tuple[int, int]) -> tuple[dict[str, float], list[str]]:
    """Check that the grab produced a real painted image of the expected size.

    Returns the measurements alongside the problems, so the caller can report
    them either way. This tier is deliberately modest about its own power: see
    the module docstring for the measurement that shows why.
    """

    if image.isNull():
        return {"levels": 0, "stdev": 0.0, "ink": 0.0}, ["the file could not be read back as an image"]
    if (image.width(), image.height()) != expected:
        return {"levels": 0, "stdev": 0.0, "ink": 0.0}, [
            f"image is {image.width()}x{image.height()}, expected {expected[0]}x{expected[1]}"
        ]

    stats = measure(image)
    problems: list[str] = []
    if stats["levels"] < MIN_GREY_LEVELS:
        problems.append(f"only {stats['levels']} grey levels (want >= {MIN_GREY_LEVELS}): nothing was drawn")
    if stats["stdev"] < MIN_TONE_STDEV:
        problems.append(f"tone deviation {stats['stdev']:.1f} (want >= {MIN_TONE_STDEV}): image looks solid")
    if stats["ink"] < MIN_INK_FRACTION:
        problems.append(f"ink {stats['ink'] * 100:.1f}% (want >= {MIN_INK_FRACTION * 100:.0f}%): image looks empty")
    return stats, problems


def capture(subject: object, output: Path) -> tuple[list[str], dict[str, float], QImage | None]:
    """Grab the widget, write the PNG, and run the image tier over what was saved.

    Reads the file back from disk rather than trusting the pixmap, because the
    failure this tier catches is a write that produced something other than what
    was handed over. An empty image and a null pixmap are different faults and
    arrive here as different return values.
    """

    pixmap = subject.grab()
    if pixmap.isNull():
        return ["grab() returned a null pixmap, so no image could be taken"], {}, None
    output.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(output), "PNG"):
        return [f"could not write {output}"], {}, None
    written = QImage(str(output))
    stats, problems = check_image(written, (pixmap.width(), pixmap.height()))
    return problems, stats, written


def describe_state(subject: object, view: str) -> list[str]:
    """What to print about what the window is showing, per view.

    Reporting the same three lines for every view would be worse than useless
    for the playlist and queue shots: a queue has no quality dropdown worth
    quoting, and a playlist's most interesting number is its row count.
    """

    if view == VIEW_PLAYLIST:
        return [
            f"mode      : {subject.mode_combo.currentText()}",
            f"rows      : {subject.playlist_table.rowCount()} videos listed",
            f"title     : {subject.title_label.text()[:70]}",
        ]
    if view == VIEW_QUEUE:
        return [
            f"mode      : {subject.mode_combo.currentText()}",
            f"rows      : {subject.queue_table.rowCount()} links queued",
        ]
    return [
        f"mode      : {subject.mode_combo.currentText()}",
        f"qualities : {subject.quality_combo.count()} options",
        f"title     : {subject.title_label.text()[:70]}",
        f"status    : {subject.status_label.text()[:70]}",
    ]


def report_hidden(subject: object) -> None:
    """Print how much of the window is scrolled out of view.

    Reported rather than assumed, so a clipped screenshot is visible in the
    output instead of having to be spotted by eye afterwards.
    """

    scroll_area = getattr(subject, "_scroll_area", None)
    if scroll_area is None:
        return
    vertical = scroll_area.verticalScrollBar().maximum()
    horizontal = scroll_area.horizontalScrollBar().maximum()
    print(f"scrolled  : {vertical}px vertical, {horizontal}px horizontal out of view")


@contextlib.contextmanager
def errors_become_reports() -> Iterator[list[str]]:
    """Turn the window's modal error boxes into recorded lines for the run's output.

    MainWindow._show_error calls QMessageBox.critical, which runs a nested event
    loop and does not return until somebody clicks OK. Rendering offscreen there
    is nobody, so any error the window decides to report - a rejected link, a
    failed fetch - turns a screenshot run into a process that hangs until
    somebody kills it. The --timeout does not help: the loop it bounds has not
    been entered yet.

    That is not hypothetical. It is how this tool lost an hour: a view whose URL
    was never filled in hit UrlValidationError, opened a message box, and sat
    there looking exactly like a slow network fetch. A tool that cannot fail is
    not a tool that works.

    So for the life of the render the error path is captured rather than shown,
    restored in a finally, and the captured text is what the caller reports.
    """

    recorded: list[str] = []
    original = MainWindow._show_error

    def record(self: MainWindow, title: str, message: str) -> None:
        recorded.append(f"{title}: {message}")

    MainWindow._show_error = record  # type: ignore[method-assign]
    try:
        yield recorded
    finally:
        MainWindow._show_error = original  # type: ignore[method-assign]


def render_window(arguments: argparse.Namespace, theme_id: str | None, app: QApplication, output: Path) -> int:
    """Photograph the main window in the requested mode."""

    window = MainWindow()
    if theme_id is not None:
        window._apply_theme(theme_id)
    apply_view(window, arguments.view, arguments)
    window.show()
    app.processEvents()

    # Fit before the fetch, so the layout is at its final height when the
    # details arrive. The playlist table and the quality list both change what is
    # visible, and a screenshot that has to be refitted afterwards is one more
    # thing to get wrong.
    size, fit_problems = fit_to_content(window, app, (arguments.width, arguments.height))
    if fit_problems:
        window.close()
        print()
        print("REJECTED - the window does not fit on screen, so the image would be clipped:")
        for problem in fit_problems:
            print(f"  - {problem}")
        return 1

    print(f"view      : {arguments.view}")
    print(f"window    : {window.width()}x{window.height()} (fitted so nothing is scrolled out of view)")
    print(f"theme     : {arguments.theme or 'window default'}")

    if arguments.no_fetch:
        print("fetch     : skipped (--no-fetch)")
    elif arguments.view not in VIEWS_THAT_FETCH:
        print("fetch     : not needed for this view")
    else:
        source = arguments.playlist if arguments.view == VIEW_PLAYLIST else arguments.video
        print(f"source    : {source}")
        print("fetching  : waiting for real details...")
        with errors_become_reports() as errors:
            window._fetch_details()
            outcome = wait_for_fetch(window, app, arguments.timeout)
        if errors:
            # Checked before the outcome, because an error means the fetch never
            # really started and the timeout would otherwise be reported as the
            # cause - which points at the network instead of at the bug.
            window.close()
            print()
            print("REJECTED - the window reported an error rather than any details:")
            for error in errors:
                print(f"  - {error}")
            return 1
        if outcome != "ok":
            window.close()
            print(f"fetch did not complete ({outcome})", file=sys.stderr)
            print(
                "The window would render empty, so no image was written. Try a\n"
                "different --video or --playlist, or check the network connection.",
                file=sys.stderr,
            )
            return 1

    # A frame after the fetch, so the layout has settled and the lists have been
    # laid out at their natural width.
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    # Fit again, because populating the details changes what is visible and the
    # first fit was taken against an empty one. Only grows: the content has
    # already been shown once, so this cannot start from a smaller window than
    # the one a user would see.
    size, fit_problems = fit_to_content(window, app, size)
    if fit_problems:
        window.close()
        print()
        print("REJECTED - the populated window does not fit, so the image would be clipped:")
        for problem in fit_problems:
            print(f"  - {problem}")
        return 1
    if (window.width(), window.height()) != size:
        print(f"refitted  : {window.width()}x{window.height()} after the details were populated")

    for line in describe_state(window, arguments.view):
        print(line)
    report_hidden(window)

    # Tier 1: is there anything here worth showing?  Decided before the grab,
    # because if the window is empty there is no point writing a file at all.
    content_problems = check_content(window, arguments.view)
    if content_problems:
        window.close()
        print()
        print("REJECTED - the window has nothing worth showing, so nothing was written:")
        for problem in content_problems:
            print(f"  - {problem}")
        if arguments.no_fetch:
            print(
                "\nThis is the expected result for --no-fetch, and the point of the flag\n"
                "is to prove this check has teeth: an empty window paints perfectly and\n"
                "still must not be committable as a screenshot."
            )
        return 1

    image_problems, stats, written = capture(window, output)
    window.close()
    return report_result(arguments, output, image_problems, stats, written)


def render_dependency_dialog(
    arguments: argparse.Namespace, theme_id: str | None, app: QApplication, output: Path
) -> int:
    """Photograph the dialog that appears before the window does.

    A window is built and thrown away first, purely for a side effect: _apply_theme
    styles the QApplication as well as the window, because native combo popups
    and message boxes need it. That is also the real sequence - cli.py builds the
    window, runs the check, and only then shows the window - so the dialog in
    this picture is themed the way a user sees it themed. Without the throwaway
    the dialog would render in whatever palette Qt invented, which is a picture
    of a program that does not exist.

    The window is closed immediately and never shown, so it contributes nothing
    to the image and nothing to the reader's screen.
    """

    theme_window = MainWindow()
    theme_window._apply_theme(theme_id or DEFAULT_THEME)
    theme_window.close()

    dialog = build_first_run_dialog()
    dialog.show()
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()

    print(f"view      : {arguments.view}")
    print(f"dialog    : {dialog.width()}x{dialog.height()}  ({dialog.windowTitle()})")
    print(f"theme     : {arguments.theme or 'window default'}")
    print("staged    : the device lookup reports no FFmpeg, so the dialog offers one")
    print("           (see the module docstring; everything else in it is live)")

    content_problems = check_content(dialog, arguments.view)
    if content_problems:
        dialog.close()
        print()
        print("REJECTED - the dialog has nothing worth showing, so nothing was written:")
        for problem in content_problems:
            print(f"  - {problem}")
        return 1

    image_problems, stats, written = capture(dialog, output)
    dialog.close()
    return report_result(arguments, output, image_problems, stats, written)


def report_result(
    arguments: argparse.Namespace,
    output: Path,
    image_problems: list[str],
    stats: dict[str, float],
    written: QImage | None,
) -> int:
    """Print the image tier's verdict and return the process exit code."""

    if written is None:
        print()
        print("REJECTED - nothing was written:")
        for problem in image_problems:
            print(f"  - {problem}")
        return 1

    shown = output.relative_to(ROOT) if output.is_relative_to(ROOT) else output
    print()
    print(f"written   : {shown}")
    print(f"size      : {written.width()}x{written.height()}  {output.stat().st_size / 1024:.0f} KB")
    print(f"levels    : {stats['levels']} distinct grey")
    print(f"stdev     : {stats['stdev']:.1f}")
    print(f"ink       : {stats['ink'] * 100:.1f}% of pixels")

    if image_problems:
        print()
        print("REJECTED - the grab itself looks wrong:")
        for problem in image_problems:
            print(f"  - {problem}")
        print(f"\nIt was still written to {shown}, so it can be looked at, but do not commit it.")
        return 1

    print()
    print(f"accepted - the {arguments.view} view holds real content and the image is intact.")
    print("NOTE: nothing has looked at this image. Open it before committing it.")
    return 0


def main() -> int:
    arguments = parse_arguments()
    if arguments.native:
        os.environ.pop("QT_QPA_PLATFORM", None)
    # Before the application is constructed: that is when the platform plugin
    # populates its font database. Without this the render is all boxes.
    fonts_directory = configure_offscreen_fonts()

    theme_id = pick_theme(arguments.theme)
    # One filename per view, so a run cannot quietly overwrite the main
    # screenshot with the playlist one. The default view keeps the name the
    # README already points at.
    output = Path(arguments.output or f"docs/screenshot{VIEW_SUFFIXES[arguments.view]}.png")
    if not output.is_absolute():
        output = ROOT / output

    # Redirect the settings file before the window is built: the theme is read
    # during construction, so redirecting afterwards would be too late.
    temporary_settings = Path(tempfile.mkdtemp(prefix="clipdock-shot-")) / "settings.json"
    settings_module.default_settings_path = lambda: temporary_settings

    configure_logging()
    app = QApplication.instance() or QApplication([])

    chosen_font = apply_ui_font(app)
    font_family, font_ratio, font_problems = check_fonts(app)
    print(f"fonts dir  : {fonts_directory or 'platform default'}")
    print(f"fonts      : {len(QFontDatabase.families())} families available")
    print(f"font      : {chosen_font or 'none applied'} -> {font_family or 'unresolved'} (glyph ratio {font_ratio:.2f})")
    if font_problems:
        print()
        print("REJECTED - the text in this window would be unreadable:")
        for problem in font_problems:
            print(f"  - {problem}")
        return 1

    if arguments.view == VIEW_FIRST_RUN:
        return render_dependency_dialog(arguments, theme_id, app, output)
    return render_window(arguments, theme_id, app, output)


if __name__ == "__main__":
    raise SystemExit(main())
