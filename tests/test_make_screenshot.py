"""Tests for the screenshot tool's verification.

These exist because the first four versions of that verification were wrong in
ways that only showed up when they were measured, or when somebody looked at
the picture.

The first rejected screenshots with fewer than 40 sampled colours, on the theory
that a blank render is flat. A window with nothing fetched measures 81 grey
levels, 7.9% ink and a tone deviation of 32.5, against 108, 10.0% and 39.6 for
a correct render, so it sailed straight through. The theory was wrong: this
window is covered in labels, borders and controls, so an empty state and a
populated state are nearly the same picture.

The second, added after that fix, produced a screenshot in which every character
was a box. Windows' offscreen platform plugin starts with an empty font
database - 0 families here against 163 native - and nothing in the window said
so. That image held real video details and nine real quality options, so the
content check passed, and a wall of boxes has plenty of tonal variety, so the
image check passed too.

The third image was clipped: a scrollbar in the picture and the footer buttons
cut off below it, because the window opens at 860x700 and its content does not
fit. No pixel statistic can see that, because a scrollbar looks like a
scrollbar.

The pattern is the same every time, and it is the reason these tests exist: a
check cannot be tightened into catching a failure mode it was never measuring.
Each one needed its own question - is the window populated, can its text be
read, did the grab work, is anything still hidden. These tests pin all four,
including the cases that got through. If a future change "simplifies" the
checks, it should find a failing test rather than a broken README.
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from tools.make_screenshot import (  # noqa: E402
    DEFAULT_QUEUE_LINKS,
    MIN_GREY_LEVELS,
    MIN_GLYPH_WIDTH_RATIO,
    MIN_INK_FRACTION,
    MIN_TONE_STDEV,
    NON_CONTENT_TITLES,
    VIEW_FIRST_RUN,
    VIEW_PLAYLIST,
    VIEW_QUEUE,
    VIEW_SUFFIXES,
    VIEW_VIDEO,
    VIEWS_THAT_FETCH,
    apply_view,
    build_first_run_dialog,
    check_content,
    check_fonts,
    check_image,
    errors_become_reports,
    fit_to_content,
    glyph_width_ratio,
    measure,
    select_mode,
)
from youtube_downloader.core.models import DownloadMode, VideoInfo, VideoQuality  # noqa: E402
from youtube_downloader.gui import dependencies as dependencies_gui_module  # noqa: E402
from youtube_downloader.gui import main_window as main_window_module  # noqa: E402
from youtube_downloader.gui.main_window import (  # noqa: E402
    PLAYLIST_COLUMN_TITLE,
    QUEUE_COLUMN_LINK,
    MainWindow,
)

ROOT = Path(__file__).resolve().parents[1]
COMMITTED_SCREENSHOT = ROOT / "docs" / "screenshot.png"


def make_info(title: str = "Big Buck Bunny", qualities: int = 1) -> VideoInfo:
    """A minimal VideoInfo shaped like what a real fetch leaves on the window."""

    return VideoInfo(
        video_id="aqz-KE-bpKQ",
        title=title,
        duration=635.0,
        thumbnail_url=None,
        qualities=tuple(
            VideoQuality(format_id=f"1{index}", height=1080 - index * 180, fps=30.0, has_audio=False)
            for index in range(qualities)
        ),
        audio_available=True,
    )


def solid_image(width: int = 860, height: int = 700, colour: str = "#202830") -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(colour))
    return image


class ContentCheckTests(unittest.TestCase):
    """The tier that actually matters: is there anything in the window to show?"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # Same isolation test_gui.py uses: building a window reads the saved
        # theme, so the suite must never touch the real settings file.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = Path(directory.name) / "settings.json"
        original_read = main_window_module.read_settings
        original_write = main_window_module.write_setting
        main_window_module.read_settings = lambda: original_read(settings_path)
        main_window_module.write_setting = lambda key, value: original_write(key, value, settings_path)
        self.addCleanup(setattr, main_window_module, "read_settings", original_read)
        self.addCleanup(setattr, main_window_module, "write_setting", original_write)

        self.window = MainWindow()
        self.addCleanup(self.window.close)

    def give_it_real_details(self, info: VideoInfo | None = None) -> None:
        info = info or make_info()
        self.window.info = info
        self.window.title_label.setText(info.title)
        for quality in info.qualities:
            self.window.quality_combo.addItem(f"{quality.height}p")

    def test_an_unfetched_window_is_not_a_screenshot(self) -> None:
        """The regression. An empty window paints perfectly and is still useless.

        Only a check against the window's own state can tell it apart from a
        real render, so this is the case that whole tier exists for.
        """

        problems = check_content(self.window, VIEW_VIDEO)

        self.assertTrue(problems, "an unfetched window must not be accepted as a screenshot")
        joined = " ".join(problems)
        self.assertIn("no video info", joined)
        self.assertIn("quality dropdown is empty", joined)
        self.assertIn("No link selected", joined)

    def test_the_pixels_of_an_empty_window_pass_the_image_check_anyway(self) -> None:
        """Measure the reason the content check above has to exist.

        This grabs the real unfetched window and runs the image tier's own floors
        over it. It clears every one of them, because this window is covered in
        labels, borders and controls: an empty state and a populated state are
        very nearly the same picture, and no threshold on pixel statistics
        separates them. If this test ever fails, the image tier has become able
        to catch an empty window, which is welcome news rather than a defect -
        but it should be a deliberate discovery, not an accident.
        """

        self.window.show()
        QApplication.processEvents()
        image = self.window.grab().toImage()
        self.assertFalse(image.isNull())

        stats = measure(image)
        self.assertGreaterEqual(
            stats["levels"],
            MIN_GREY_LEVELS,
            f"an empty window now measures only {stats['levels']} grey levels, so the "
            f"image tier can reject it on its own; the content tier may be redundant",
        )
        self.assertGreaterEqual(stats["stdev"], MIN_TONE_STDEV)
        self.assertGreaterEqual(stats["ink"], MIN_INK_FRACTION)

        # And the same window is still refused, by the check that can tell.
        self.assertTrue(check_content(self.window, VIEW_VIDEO))

    def test_a_window_holding_real_details_is_accepted(self) -> None:
        self.give_it_real_details()

        self.assertEqual(check_content(self.window, VIEW_VIDEO), [])

    def test_info_without_qualities_is_rejected(self) -> None:
        """A result that parsed but offers nothing cannot fill the dropdown."""

        self.give_it_real_details(make_info(qualities=0))

        problems = check_content(self.window, VIEW_VIDEO)
        self.assertTrue(problems)
        self.assertIn("no quality options", " ".join(problems))

    def test_a_populated_dropdown_with_an_empty_title_is_rejected(self) -> None:
        """Guards the wording-independent half of the check.

        Deliberately builds a window that has real formats in it but still shows
        a placeholder title. A check that only looked at the dropdown would let
        this through, and the resulting image would look populated and read as
        empty.
        """

        self.give_it_real_details()
        self.window.title_label.setText("")

        problems = check_content(self.window, VIEW_VIDEO)
        self.assertTrue(problems)
        self.assertIn("blank", " ".join(problems))

    def test_every_placeholder_title_is_rejected(self) -> None:
        for placeholder in sorted(NON_CONTENT_TITLES):
            with self.subTest(placeholder=placeholder):
                self.give_it_real_details()
                self.window.title_label.setText(placeholder)

                problems = check_content(self.window, VIEW_VIDEO)
                self.assertTrue(problems, f"{placeholder!r} should not pass")
                self.assertIn("empty-state placeholder", " ".join(problems))

    def test_the_placeholder_titles_still_exist_in_the_window_source(self) -> None:
        """Pin the list to the literals it mirrors.

        NON_CONTENT_TITLES is a copy of strings that live inline in
        main_window.py. If that file is reworded, this copy goes stale and the
        check quietly stops recognising the empty state. Reading the source here
        turns that silent decay into a failing test.
        """

        source = (ROOT / "youtube_downloader" / "gui" / "main_window.py").read_text(encoding="utf-8")

        for placeholder in sorted(NON_CONTENT_TITLES):
            with self.subTest(placeholder=placeholder):
                self.assertIn(
                    f'"{placeholder}"',
                    source,
                    f"{placeholder!r} is listed as a placeholder but no longer appears in "
                    f"main_window.py; update NON_CONTENT_TITLES to match the window",
                )


class ImageCheckTests(unittest.TestCase):
    """The weaker tier: catches a grab that went wrong, nothing more."""

    def test_a_null_image_is_rejected(self) -> None:
        stats, problems = check_image(QImage(), (860, 700))

        self.assertTrue(problems)
        self.assertIn("could not be read back", " ".join(problems))
        self.assertEqual(stats["levels"], 0)

    def test_an_image_of_the_wrong_size_is_rejected(self) -> None:
        stats, problems = check_image(solid_image(400, 300), (860, 700))

        self.assertTrue(problems)
        self.assertIn("400x300", " ".join(problems))
        self.assertEqual(stats["levels"], 0)

    def test_a_solid_colour_image_is_rejected(self) -> None:
        """The correct negative control for this tier.

        An empty *window* is not a blank *image*, which is why the content tier
        exists. A genuinely blank image is a solid fill, and that is what these
        floors are for: a null pixmap, an unwritten buffer or a stylesheet that
        never applied all collapse to something like this.
        """

        image = solid_image()

        stats, problems = check_image(image, (image.width(), image.height()))

        self.assertTrue(problems)
        self.assertEqual(stats["levels"], 1, "a solid fill has exactly one grey level")
        self.assertEqual(stats["stdev"], 0.0, "a solid fill has no spread")
        self.assertEqual(stats["ink"], 0.0, "a solid fill is entirely its own dominant value")

    def test_measure_counts_every_pixel_not_a_sample(self) -> None:
        """Two pixels differing in one corner must both be seen.

        The original implementation walked a 48x48 grid and reported 13 distinct
        colours for a good render, because it was measuring which points it
        happened to sample. This pins that the measurement covers the image.
        """

        image = solid_image(100, 100, "#101010")
        image.setPixelColor(99, 99, QColor("#f0f0f0"))

        stats = measure(image)

        self.assertEqual(stats["levels"], 2)
        self.assertGreater(stats["stdev"], 0.0)

    def test_the_floors_sit_below_what_a_real_render_measures(self) -> None:
        """Calibration, checked against the artefact actually in the repository.

        If the committed screenshot ever stops clearing the floors, the floors
        are wrong rather than the screenshot, and this says so directly.
        """

        if not COMMITTED_SCREENSHOT.exists():
            self.skipTest("docs/screenshot.png is not present in this checkout")

        image = QImage(str(COMMITTED_SCREENSHOT))
        self.assertFalse(image.isNull(), "the committed screenshot could not be read")
        stats = measure(image)

        self.assertGreater(
            stats["levels"],
            MIN_GREY_LEVELS,
            f"committed screenshot measures {stats['levels']} levels, at or below the "
            f"floor of {MIN_GREY_LEVELS}",
        )
        self.assertGreater(stats["stdev"], MIN_TONE_STDEV)
        self.assertGreater(stats["ink"], MIN_INK_FRACTION)

    def test_the_floors_reject_an_image_of_the_wrong_shape(self) -> None:
        """A valid image at the wrong size is still not the render that was asked for."""

        stats, problems = check_image(solid_image(860, 700), (400, 300))

        self.assertTrue(problems)
        self.assertIn("expected 400x300", " ".join(problems))
        self.assertEqual(stats["levels"], 0, "size is checked before the pixels are read")


class FontCheckTests(unittest.TestCase):
    """The tier added after the first screenshot came out as a wall of boxes.

    Worth recording why it exists, because it is the clearest example yet of a
    check that had to be added rather than tightened: the unreadable screenshot
    held real video info and nine real quality options, so the content check
    passed, and a screen full of tofu boxes has plenty of tonal variety, so the
    image check passed. Both reported a good screenshot of something nobody can
    read. Windows' offscreen platform plugin starts with an empty font
    database, and nothing in the window said so.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_an_empty_font_database_is_reported_as_a_failure(self) -> None:
        """The exact condition that produced the boxes."""

        with mock.patch.object(QFontDatabase, "families", return_value=[]):
            family, ratio, problems = check_fonts(self.app)

        self.assertTrue(problems, "an empty font database must be a failure")
        self.assertIn("no fonts", " ".join(problems))
        self.assertIn("QT_QPA_FONTDIR", " ".join(problems))
        self.assertEqual(ratio, 0.0)

    def test_a_font_without_letterforms_is_reported_as_a_failure(self) -> None:
        """A database that is populated but unusable is the subtler version."""

        with mock.patch.object(QFontDatabase, "families", return_value=["Something"]), mock.patch(
            "tools.make_screenshot.glyph_width_ratio", return_value=1.0
        ):
            _family, _ratio, problems = check_fonts(self.app)

        self.assertTrue(problems)
        self.assertIn("no letterforms", " ".join(problems))

    def test_the_real_startup_path_yields_readable_glyphs(self) -> None:
        """End-to-end, in a fresh interpreter, which is the only honest way.

        make_screenshot repairs the font database by setting QT_QPA_FONTDIR when
        main() asks it to, and that only takes effect if no QApplication exists
        yet. In a full-suite run test_gui builds one long before this module is
        imported, so checking fonts in-process here would measure the suite's
        environment rather than the tool's, and would fail for a reason that has
        nothing to do with the code. A subprocess reproduces the real order:
        ask for the fonts, then construct the application.
        """

        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from PySide6.QtGui import QFontDatabase
            from PySide6.QtWidgets import QApplication
            from tools.make_screenshot import (
                apply_ui_font, check_fonts, configure_offscreen_fonts,
            )
            fonts = configure_offscreen_fonts()
            app = QApplication.instance() or QApplication([])
            apply_ui_font(app)
            family, ratio, problems = check_fonts(app)
            print("FONTSDIR", fonts)
            print("FAMILIES", len(QFontDatabase.families()))
            print("FAMILY", family)
            print("RATIO", ratio)
            print("PROBLEMS", problems)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        output = completed.stdout
        self.assertIn("PROBLEMS []", output, f"the real startup path reports problems:\n{output}")

        families = int(next(line for line in output.splitlines() if line.startswith("FAMILIES")).split()[1])
        ratio = float(next(line for line in output.splitlines() if line.startswith("RATIO")).split()[1])

        self.assertGreater(families, 0, "the offscreen platform has no fonts to draw with")
        self.assertGreaterEqual(
            ratio,
            MIN_GLYPH_WIDTH_RATIO,
            f"glyph width ratio is {ratio:.2f}, at or below the floor of {MIN_GLYPH_WIDTH_RATIO}: "
            f"every character would render as the same box",
        )

    def test_importing_the_tool_does_not_change_the_font_database(self) -> None:
        """Importing must not fix the environment as a side effect.

        The test suite imports this module, and a font database appearing as a
        side effect would change how test_gui renders. That module has been
        drawing boxes and has tests asserting on painted glyphs, so quietly
        changing its environment is not a harmless gift - it would alter tests
        nobody asked to alter. The repair is requested explicitly instead.
        """

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys
                    sys.path.insert(0, {str(ROOT)!r})
                    import tools.make_screenshot
                    from PySide6.QtGui import QFontDatabase
                    from PySide6.QtWidgets import QApplication
                    app = QApplication.instance() or QApplication([])
                    print("FAMILIES", len(QFontDatabase.families()))
                    """
                ),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        families = int(
            next(line for line in completed.stdout.splitlines() if line.startswith("FAMILIES")).split()[1]
        )
        self.assertEqual(
            families,
            0,
            "importing make_screenshot set QT_QPA_FONTDIR; the repair must happen only "
            "when main() asks for it, so importing it cannot alter another test module's "
            "rendering",
        )


class FitTests(unittest.TestCase):
    """The tier added after the first screenshot came out with its footer cut off.

    The window opens at 860x700 and its content does not fit: the row of buttons
    along the bottom hangs below the fold behind a scrollbar. Nothing about that
    shows up in the pixels - a scrollbar looks exactly like a scrollbar - so it
    has to be asked of the window.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = Path(directory.name) / "settings.json"
        original_read = main_window_module.read_settings
        original_write = main_window_module.write_setting
        main_window_module.read_settings = lambda: original_read(settings_path)
        main_window_module.write_setting = lambda key, value: original_write(key, value, settings_path)
        self.addCleanup(setattr, main_window_module, "read_settings", original_read)
        self.addCleanup(setattr, main_window_module, "write_setting", original_write)

        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.show()
        self.app.processEvents()

    def hidden_pixels(self) -> tuple[int, int]:
        scroll_area = self.window._scroll_area
        return (
            scroll_area.verticalScrollBar().maximum(),
            scroll_area.horizontalScrollBar().maximum(),
        )

    def test_the_default_size_really_does_hide_part_of_the_window(self) -> None:
        """The reason this tier exists, asserted rather than remembered.

        If the window ever grows a default size that fits, this fails and the
        fitting becomes unnecessary - which would be worth knowing rather than
        leaving the growth logic in place forever.
        """

        self.window.resize(860, 700)
        self.app.processEvents()

        vertical, _horizontal = self.hidden_pixels()
        self.assertGreater(
            vertical,
            0,
            "the window now fits at 860x700, so fit_to_content has nothing to do and can be removed",
        )

    def test_fitting_leaves_nothing_scrolled_out_of_view(self) -> None:
        (width, height), problems = fit_to_content(self.window, self.app, (860, 700))

        self.assertEqual(problems, [])
        self.assertEqual((width, height), (self.window.width(), self.window.height()))
        self.assertEqual(self.hidden_pixels(), (0, 0), "content is still hidden after fitting")

    def test_fitting_grows_rather_than_shrinks(self) -> None:
        """A size that already fits is left alone.

        Growing unconditionally would pad the image with empty window, which is
        its own kind of wrong picture.
        """

        (_width, height), problems = fit_to_content(self.window, self.app, (1000, 1400))

        self.assertEqual(problems, [])
        self.assertEqual((self.window.width(), self.window.height()), (1000, 1400))

    def test_content_too_tall_for_the_ceiling_is_reported(self) -> None:
        """A ceiling lower than the content must fail rather than clip quietly."""

        _size, problems = fit_to_content(self.window, self.app, (860, 700), ceiling=(860, 300))

        self.assertTrue(problems)
        self.assertIn("still does not fit", " ".join(problems))


class ViewTests(unittest.TestCase):
    """The other three views, and the checks that can tell each one is populated.

    Adding a view is the easy part. The part that needs writing down is that
    every one of them needs its own content check, for the same reason the first
    one did: a populated playlist and an empty one are the same labels, the same
    dropdowns and the same empty columns. Nothing about "is anything here" is
    visible in the picture, so each view is asked its own question about its own
    state.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_path = Path(directory.name) / "settings.json"
        original_read = main_window_module.read_settings
        original_write = main_window_module.write_setting
        main_window_module.read_settings = lambda: original_read(settings_path)
        main_window_module.write_setting = lambda key, value: original_write(key, value, settings_path)
        self.addCleanup(setattr, main_window_module, "read_settings", original_read)
        self.addCleanup(setattr, main_window_module, "write_setting", original_write)

        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.show()
        self.app.processEvents()
        # Whatever the calls below need, and nothing more.
        self.arguments = argparse.Namespace(
            view=VIEW_VIDEO,
            video="https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            playlist="https://www.youtube.com/playlist?list=PLav47HAVZMjnTFVZL-aImCQIC0uLZtNCz",
        )

    # --- playlist -----------------------------------------------------------

    def switch_to(self, mode: DownloadMode) -> None:
        select_mode(self.window, mode)
        self.app.processEvents()

    def test_an_empty_playlist_is_rejected(self) -> None:
        """The regression for this view: a playlist mode with no rows fetched."""

        self.switch_to(DownloadMode.PLAYLIST)

        problems = check_content(self.window, VIEW_PLAYLIST)

        self.assertTrue(problems, "a playlist view with no rows must not be accepted")
        self.assertIn("not showing a playlist", " ".join(problems))

    def test_a_populated_playlist_is_accepted(self) -> None:
        self.switch_to(DownloadMode.PLAYLIST)
        self.fill_playlist_rows(3, with_titles=True)

        self.assertEqual(check_content(self.window, VIEW_PLAYLIST), [])

    def fill_playlist_rows(self, count: int, with_titles: bool) -> None:
        table = self.window.playlist_table
        table.setRowCount(count)
        for row in range(count):
            title = f"Blender Open Movie {row}" if with_titles else ""
            table.setItem(row, PLAYLIST_COLUMN_TITLE, QTableWidgetItem(title))

    def test_playlist_rows_with_blank_titles_are_rejected(self) -> None:
        """Rows that arrived without titles.

        The row count alone would pass this, and the picture would show a table
        of empty rows under a heading claiming eighteen videos - the exact shape
        of the failure this whole file keeps rediscovering.
        """

        self.switch_to(DownloadMode.PLAYLIST)
        self.fill_playlist_rows(4, with_titles=False)

        problems = check_content(self.window, VIEW_PLAYLIST)

        self.assertTrue(problems)
        self.assertIn("every playlist row is blank", " ".join(problems))

    # --- queue --------------------------------------------------------------

    def test_a_queue_where_links_failed_to_parse_is_rejected(self) -> None:
        """Unparseable links are dropped on purpose, so the loss is silent.

        A person pasting a mix of links and notes should not be blocked by a
        typo, which is right. It also means a queue that quietly lost three of
        four rows would render as a perfectly respectable, nearly empty table.
        """

        self.switch_to(DownloadMode.QUEUE)

        problems = check_content(self.window, VIEW_QUEUE)

        self.assertTrue(problems)
        self.assertIn("did not parse", " ".join(problems))

    def test_a_queue_holding_every_link_is_accepted(self) -> None:
        self.switch_to(DownloadMode.QUEUE)
        apply_view(self.window, VIEW_QUEUE, self.arguments)

        self.assertEqual(self.window.queue_table.rowCount(), len(DEFAULT_QUEUE_LINKS))
        self.assertEqual(check_content(self.window, VIEW_QUEUE), [])

    def test_the_queue_links_are_distinct_real_looking_urls(self) -> None:
        """A duplicated link would quietly produce a shorter table than expected.

        The check compares the row count against the number of links, so a typo
        that made two lines identical would show up as a mismatch rather than as
        a plausible picture. Worth asserting here rather than discovering it in
        a screenshot nobody re-rendered.
        """

        self.assertEqual(len(set(DEFAULT_QUEUE_LINKS)), len(DEFAULT_QUEUE_LINKS))
        for link in DEFAULT_QUEUE_LINKS:
            with self.subTest(link=link):
                self.assertTrue(link.startswith("https://www.youtube.com/watch?v="))
                self.assertGreater(len(link.rsplit("=", 1)[-1]), 10, f"{link} has no video id")

    # --- first run ----------------------------------------------------------

    def test_the_dependency_dialog_says_something(self) -> None:
        dialog = build_first_run_dialog()
        self.addCleanup(dialog.close)

        self.assertEqual(check_content(dialog, VIEW_FIRST_RUN), [])

    def test_a_dialog_with_an_empty_explanation_is_rejected(self) -> None:
        """A correctly laid out, correctly themed, entirely blank rectangle.

        This is the exact shape of screenshot that got shipped three times while
        this file was being written, so it is worth a test that it cannot happen
        in the fourth view either.
        """

        dialog = build_first_run_dialog()
        self.addCleanup(dialog.close)
        dialog.detail_label.setText("")

        problems = check_content(dialog, VIEW_FIRST_RUN)

        self.assertTrue(problems)
        self.assertIn("not an explanation", " ".join(problems))

    def test_the_dialog_does_not_claim_it_found_what_it_is_offering(self) -> None:
        """The staging in build_first_run_dialog must be doing its job.

        This machine has FFmpeg on PATH, on purpose, to test device-wide
        detection. Left alone, the dialog would say it found FFmpeg and offer to
        download FFmpeg in the same paragraph - a self-contradicting picture,
        and the sort that makes a program look like a scam rather than a tool.
        """

        dialog = build_first_run_dialog()
        self.addCleanup(dialog.close)
        detail = dialog.detail_label.text()

        self.assertIn("could not find FFmpeg", detail)
        self.assertNotIn("found FFmpeg already in use", detail)

    def test_the_staged_lookup_is_not_left_patched_behind(self) -> None:
        """The patch is restored, so the rest of the process sees the real device.

        build_first_run_dialog patches locate_device_ffmpeg and restores it in a
        finally. A version that forgot would make every later check in the same
        process believe the computer has no FFmpeg, which is a plausible way to
        ship a quietly wrong picture - or to misreport this machine's own
        dependency state in a test run.
        """

        original = dependencies_gui_module.locate_device_ffmpeg

        build_first_run_dialog().close()

        self.assertIs(
            dependencies_gui_module.locate_device_ffmpeg,
            original,
            "build_first_run_dialog left its device-lookup patch in place",
        )

    # --- shared plumbing ----------------------------------------------------

    def test_only_the_views_that_need_youtube_are_marked_as_fetching(self) -> None:
        """The queue builds its rows by parsing text; the dialog fetches nothing.

        Sending those two through the fetch path would produce a slow failure
        rather than an honest picture, so the set that does fetch is named
        explicitly and pinned here.
        """

        self.assertEqual(VIEWS_THAT_FETCH, frozenset({VIEW_VIDEO, VIEW_PLAYLIST}))

    def test_each_view_writes_to_its_own_filename(self) -> None:
        """No view may overwrite another's image.

        All four ran from one tool with one default; a shared filename would have
        meant the last view to run silently replaced the hero screenshot that the
        README points at, and nothing would have said so.
        """

        names = {view: f"docs/screenshot{suffix}.png" for view, suffix in VIEW_SUFFIXES.items()}

        self.assertEqual(names[VIEW_VIDEO], "docs/screenshot.png")
        self.assertEqual(len(set(names.values())), len(names), f"two views share a filename: {names}")

    def test_selecting_a_mode_the_window_does_not_offer_is_an_error(self) -> None:
        """A typo must stop the run, not fall back to whatever mode is first.

        Falling back would render a video window while the output claimed to be a
        playlist, which is precisely the kind of quietly wrong picture this tool
        exists to avoid.
        """

        with self.assertRaises(SystemExit):
            select_mode(self.window, mock.Mock(value="not-a-mode"))

    def test_the_video_view_dispatches_to_the_video_check(self) -> None:
        """The default must not have been re-pointed at another view's question."""

        self.give_video_details()

        self.assertEqual(check_content(self.window, VIEW_VIDEO), [])

    def test_the_video_view_fills_in_the_url_box(self) -> None:
        """The bug that cost an hour, pinned.

        apply_view originally set the URL for the playlist view and the queue
        view and nothing for the single-video view, so the window was asked to
        fetch an empty link. The result was not an exception: UrlValidationError
        opens a modal message box, and a modal message box with nobody to click
        OK hangs the process. It looked exactly like a slow network fetch, which
        is why it was not obvious.

        Every view that fetches must therefore leave a URL in the box, asserted
        here rather than discovered the same way again.
        """

        apply_view(self.window, VIEW_VIDEO, self.arguments)

        self.assertEqual(self.window.url_edit.text(), self.arguments.video)

    def test_the_playlist_view_fills_in_the_url_box(self) -> None:
        apply_view(self.window, VIEW_PLAYLIST, self.arguments)

        self.assertEqual(self.window.url_edit.text(), self.arguments.playlist)

    def test_a_window_error_is_recorded_instead_of_opening_a_modal_box(self) -> None:
        """A modal message box must not be able to hang a headless render.

        QMessageBox.critical runs a nested event loop and waits for a click that
        cannot come. Verified here by calling the real _show_error inside the
        context manager: it returns at all, which is the property that matters,
        and the text it would have shown is what comes back.
        """

        window = MainWindow()
        self.addCleanup(window.close)

        with errors_become_reports() as recorded:
            window._show_error("Invalid link", "that is not a YouTube link")

        self.assertEqual(recorded, ["Invalid link: that is not a YouTube link"])

    def test_the_error_capture_is_removed_afterwards(self) -> None:
        """Restored in a finally, so nothing else in the process is affected."""

        original = MainWindow._show_error

        with errors_become_reports():
            self.assertIsNot(MainWindow._show_error, original)

        self.assertIs(MainWindow._show_error, original)

    def test_an_error_during_the_error_capture_is_still_restored(self) -> None:
        original = MainWindow._show_error

        with self.assertRaises(RuntimeError):
            with errors_become_reports():
                raise RuntimeError("something in the render went wrong")

        self.assertIs(
            MainWindow._show_error,
            original,
            "a crash inside the capture left the window's error path replaced",
        )

    def give_video_details(self) -> None:
        info = make_info()
        self.window.info = info
        self.window.title_label.setText(info.title)
        for quality in info.qualities:
            self.window.quality_combo.addItem(f"{quality.height}p")


if __name__ == "__main__":
    unittest.main()
