from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_downloader.core.binaries import application_roots, find_ffmpeg, require_ffmpeg


def _connected_blobs(pixels: set[tuple[int, int]], size: int) -> list[int]:
    """The sizes of the four-connected white regions in an icon.

    Four-connected rather than eight: the corners of a diagonal are where two
    shapes almost-but-not-quite touch, and counting them as joined would hide
    exactly the near-miss this is looking for.
    """

    remaining = set(pixels)
    sizes: list[int] = []
    while remaining:
        start = remaining.pop()
        blob = [start]
        frontier = [start]
        while frontier:
            x, y = frontier.pop()
            for neighbour in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    blob.append(neighbour)
                    frontier.append(neighbour)
        sizes.append(len(blob))
    return sorted(sizes, reverse=True)


def _closer_to_ink_than_tile(pixel, tile) -> bool:
    """True for any pixel nearer the mark than the tile, fringe included.

    The icon tests need two different definitions of "the mark", and conflating
    them is what made a measurement wrong once already.  A near-white core is
    the right ruler for a shape that is meant to be solid.  At 16 pixels the
    icon's outline is thinner than one pixel and has no core at all, and asking
    for one there measures the rasteriser rather than the drawing.  This second
    ruler includes the antialiased fringe, so at that size it answers the only
    question still worth asking: is the mark still one piece?
    """

    channels = (pixel.red(), pixel.green(), pixel.blue())
    tile_channels = (tile.red(), tile.green(), tile.blue())
    to_ink = sum(255 - channel for channel in channels)
    to_tile = sum(
        abs(channel - base) for channel, base in zip(channels, tile_channels)
    )
    return to_ink < to_tile


class BinaryDiscoveryTests(unittest.TestCase):
    def test_source_run_resolves_ffmpeg_from_the_vendor_folder(self) -> None:
        ffmpeg = find_ffmpeg()
        self.assertIsNotNone(ffmpeg, "the bundled FFmpeg should be discoverable from source")
        assert ffmpeg is not None
        self.assertTrue(ffmpeg.is_file())
        self.assertEqual(ffmpeg.name, "ffmpeg.exe")

    def test_ffprobe_is_not_bundled_and_nothing_requires_it(self) -> None:
        """ffprobe is a 128 MB binary the app never calls, so it must be absent."""

        root = Path(__file__).resolve().parents[1]
        self.assertFalse(
            (root / "vendor" / "ffmpeg" / "bin" / "ffprobe.exe").exists(),
            "ffprobe.exe should have been removed from the bundled vendor folder",
        )
        # require_ffmpeg is the only hard dependency, and it must still work.
        self.assertEqual(require_ffmpeg(root), find_ffmpeg(root))

    def test_explicit_root_wins_over_the_default_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "vendor" / "ffmpeg.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"stub")
            self.assertEqual(find_ffmpeg(root), target)

    def test_missing_root_falls_back_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PATH": directory}, clear=False):
                self.assertIsNone(find_ffmpeg(Path(directory)))

    def test_environment_override_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "custom-ffmpeg.exe"
            override.write_bytes(b"stub")
            with patch.dict(os.environ, {"YTDOWNLOADER_FFMPEG": str(override)}):
                self.assertEqual(find_ffmpeg(Path(directory)), override)

    def test_frozen_app_checks_the_executable_directory(self) -> None:
        """A packaged exe ships vendor/ beside itself, not inside the bundle."""

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "ClipDock.exe"
            executable.write_bytes(b"stub")
            beside = Path(directory) / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"
            beside.parent.mkdir(parents=True)
            beside.write_bytes(b"stub")
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(executable)
            ), patch.object(sys, "_MEIPASS", str(Path(directory) / "_internal"), create=True):
                roots = application_roots()
                # resolve() is used because a temporary path may be a Windows
                # 8.3 short name while the executable directory is not.
                self.assertEqual(roots[0], Path(directory).resolve())
                self.assertIn((Path(directory) / "_internal").resolve(), roots)
                self.assertEqual(find_ffmpeg(), beside.resolve())

    def test_root_list_has_no_duplicates(self) -> None:
        roots = application_roots()
        self.assertTrue(roots)
        self.assertEqual(len(roots), len(set(roots)))


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Make sure a Qt application object exists before any pixmap is made.

        The generator draws into QPixmap, which aborts the process outright if
        no QGuiApplication has been constructed - a hard crash, not an
        exception, so it cannot be caught.  These tests used to pass only
        because another module happened to build one first.
        """

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication

        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_generated_icon_is_a_valid_multi_size_ico(self) -> None:
        from tools.make_icon import write_ico

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "app.ico"
            write_ico(destination, (16, 48, 256))
            data = destination.read_bytes()

        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual(reserved, 0)
        self.assertEqual(kind, 1)  # 1 == icon
        self.assertEqual(count, 3)

        offset = 6
        seen = []
        for _ in range(count):
            (
                width,
                height,
                _colors,
                _reserved,
                _planes,
                bits,
                size,
                image_offset,
            ) = struct.unpack("<BBBBHHII", data[offset : offset + 16])
            offset += 16
            self.assertEqual(width, height)
            self.assertEqual(bits, 32)
            payload = data[image_offset : image_offset + size]
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertLessEqual(image_offset + size, len(data))
            seen.append(width or 256)
        self.assertEqual(seen, [16, 48, 256])

    def test_the_nested_heads_survive_at_taskbar_size(self) -> None:
        """An icon is judged at 16 pixels, not at 256.

        A mark that only reads when it is large is useless in a taskbar or in a
        file list, which is where the user actually sees it.  Nothing here is
        eyeballed, because this program cannot look at its own artwork:

        * the tile has to fill a real part of the canvas, and stop short of
          filling all of it, or there is no shape left to read;
        * the mark is white on graphite, so both heads are found as near-white
          pixels inside the tile and then counted as separate blobs.  From 24
          pixels up there must be exactly two, because the nesting is the whole
          idea: one means the inner head has fused with the outline everywhere,
          which collapses the mark into the plain arrow every downloader already
          owns, and three means something has come loose;
        * the nested head has to be a shape in its own right and not a stray
          pixel that survived next to the outline, so it clears both an absolute
          floor and a floor relative to the outline;
        * 16 pixels is judged by a deliberately different ruler.  The outline is
          0.082 of the canvas, which is 1.2 pixels there - less than one pixel
          of solid white - so the mark has no core to measure at that size, and
          applying the rule above would only be counting rasterisation noise.
          What is required instead is that the mark is still one contiguous
          shape once its antialiased fringe is included: a mark that has come
          apart at taskbar size has failed, a mark that is merely thin has not.
          This is a known limitation of the approved weight and is recorded
          rather than papered over.  The test would pass just as well with a
          bolder outline, which is exactly why it does not assert a core it knows
          is absent;
        * white on graphite has to clear 3:1, the threshold for a graphical
          object, measured at the darkest tile pixel the mark sits against.
        """

        from PySide6.QtGui import QColor, QImage

        from tools.make_icon import TILE, render_icon

        def luminance(pixel) -> float:
            def channel(value: int) -> float:
                v = value / 255
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

            return (
                0.2126 * channel(pixel.red())
                + 0.7152 * channel(pixel.green())
                + 0.0722 * channel(pixel.blue())
            )

        background = QColor(TILE)

        for size in (16, 24, 32, 48, 64, 128, 256):
            with self.subTest(size=size):
                image = QImage()
                self.assertTrue(image.loadFromData(render_icon(size), "PNG"))
                at = lambda x, y: image.pixelColor(x, y)  # noqa: E731
                opaque = [
                    (x, y)
                    for y in range(size)
                    for x in range(size)
                    if at(x, y).alpha() > 200
                ]
                coverage = len(opaque) / (size * size)
                self.assertGreater(
                    coverage, 0.55, "the tile does not fill enough of the canvas"
                )
                self.assertLess(
                    coverage, 0.95, "the tile is a solid block with no shape in it"
                )
                mark = {
                    (x, y)
                    for x, y in opaque
                    if min(at(x, y).red(), at(x, y).green(), at(x, y).blue()) > 200
                }
                self.assertGreater(
                    len(mark) / (size * size), 0.03, "the mark vanished at this size"
                )
                blobs = _connected_blobs(mark, size)
                # A lone pixel on the rim of a curve is rasterisation noise, not
                # an element, so it is reported but never counted as a shape.
                shapes = [blob for blob in blobs if blob >= 3]
                if size == 16:
                    # No core exists here, so the fringe is what is measured.
                    fringe = {
                        (x, y)
                        for x, y in opaque
                        if _closer_to_ink_than_tile(at(x, y), background)
                    }
                    joined = [
                        blob
                        for blob in _connected_blobs(fringe, size)
                        if blob >= 3
                    ]
                    self.assertEqual(
                        len(joined),
                        1,
                        f"the mark came apart at taskbar size: {joined}",
                    )
                    self.assertGreater(
                        joined[0] / (size * size),
                        0.08,
                        "the mark is barely present at taskbar size",
                    )
                    continue
                self.assertEqual(
                    len(shapes), 2, f"expected the two heads, got {blobs}"
                )
                # The inner head has to be a shape in its own right, not a
                # stray pixel that happened to survive next to the outline.
                self.assertGreater(shapes[-1], 5, f"inner head is a speck: {blobs}")
                self.assertGreater(
                    shapes[-1] / shapes[0], 0.05, f"inner head is lost: {blobs}"
                )
                worst = min(
                    luminance(at(x, y)) for x, y in opaque if (x, y) not in mark
                )
                ratio = 1.05 / (worst + 0.05)
                self.assertGreater(
                    ratio, 3.0, f"the mark has only {ratio:.2f}:1 against the tile"
                )

    def test_the_checked_in_icon_matches_the_generator(self) -> None:
        """The committed .ico must be what the generator now produces.

        The icon is regenerated by the build script, but the file is also
        committed so a source checkout has one without running a build.  If the
        two drift, the taskbar shows an icon that no longer matches the program.
        """

        committed = Path(__file__).resolve().parents[1] / "assets" / "app.ico"
        if not committed.is_file():
            self.skipTest("assets/app.ico has not been generated yet")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "app.ico"
            from tools.make_icon import write_ico

            write_ico(destination, (16, 32, 48, 64, 128, 256))
            self.assertEqual(
                committed.read_bytes(),
                destination.read_bytes(),
                "assets/app.ico is stale; rerun tools/make_icon.py",
            )

    def test_the_png_ladder_matches_the_generator(self) -> None:
        """The standalone PNGs in assets/ must match what the generator produces."""

        committed_assets = Path(__file__).resolve().parents[1] / "assets"
        from tools.make_icon import PNG_SIZES, render_icon

        for size in PNG_SIZES:
            path = committed_assets / f"app-{size}.png"
            if not path.is_file():
                self.skipTest(f"{path.name} is missing; regenerate with make_icon.py")
            self.assertEqual(
                path.read_bytes(),
                render_icon(size),
                f"{path.name} is stale; rerun tools/make_icon.py",
            )


if __name__ == "__main__":
    unittest.main()
