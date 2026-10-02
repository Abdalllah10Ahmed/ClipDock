from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

# One flat graphite, and white on it.  The icon before this was a
# violet-to-indigo gradient, and a saturated gradient tile is the loudest
# signal that an icon belongs to the downloader-crack genre - it read as a scam
# before the shape was ever discussed.  Flat also means there is exactly one
# background tone, so the test can compare every pixel against one colour.
TILE = "#14171c"
INK = "#ffffff"

# The sizes the .ico carries.  256 is a ceiling imposed by the format rather than
# chosen here: an ICO directory entry stores width and height in a single byte
# each, where 0 means 256, so the format cannot describe a 512-pixel entry at
# all.  One .ico holding several sizes is not one image - Windows picks the
# closest entry to whatever it is painting, which is how a single file serves a
# 16-pixel taskbar and a 256-pixel Explorer tile.
ICO_SIZES = (16, 32, 48, 64, 128, 256)

# The standalone PNGs.  Anything the Windows shell draws is capped at 256 by the
# limit above, so a larger master has to be its own file: a web page, a document
# or a splash screen cannot upscale a 256-pixel source and call the result sharp.
PNG_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512, 1024)


def _rounded_polygon(points, radius: float) -> QPainterPath:
    """A polygon with every corner replaced by a curve tangent to both edges.

    This is what keeps the arrowhead from being a spike.  The straight edges
    stay straight; only the corners round off, and the radius is clamped per
    corner so that neighbouring corners cannot eat each other on a small head.
    """

    count = len(points)
    entries: list[tuple[float, float]] = []
    exits: list[tuple[float, float]] = []
    for index, (x, y) in enumerate(points):
        previous = points[(index - 1) % count]
        following = points[(index + 1) % count]
        ax, ay = x - previous[0], y - previous[1]
        bx, by = following[0] - x, following[1] - y
        la = math.hypot(ax, ay) or 1.0
        lb = math.hypot(bx, by) or 1.0
        r = min(radius, la / 2, lb / 2)
        entries.append((x - ax / la * r, y - ay / la * r))
        exits.append((x + bx / lb * r, y + by / lb * r))
    path = QPainterPath()
    path.moveTo(*exits[0])
    for index in range(1, count + 1):
        corner = index % count
        path.lineTo(*entries[corner])
        path.quadTo(*points[corner], *exits[corner])
    path.closeSubpath()
    return path


def _arrow_head(
    tip: tuple[float, float],
    direction: tuple[float, float],
    length: float,
    width: float,
    roundness: float,
) -> QPainterPath:
    """A download arrowhead whose axis follows ``direction``.

    The two base corners are rounded as well as the point, which is what
    separates this from the flat-sided triangle in a download badge.
    """

    dx, dy = direction
    norm = math.hypot(dx, dy) or 1.0
    dx, dy = dx / norm, dy / norm
    nx, ny = -dy, dx
    base_x, base_y = tip[0] - dx * length, tip[1] - dy * length
    return _rounded_polygon(
        [
            tip,
            (base_x - nx * width / 2, base_y - ny * width / 2),
            (base_x + nx * width / 2, base_y + ny * width / 2),
        ],
        roundness,
    )


def _stroke(painter: QPainter, path: QPainterPath, ink: QColor, width: float) -> None:
    """Stroke a closed outline with round caps and round joins.

    Round joins are the reason the outer head is stroked rather than filled: the
    inside of a filled triangle's corner is a mitre, and a mitre is exactly the
    sharp edge this icon must not have.  The save and restore matter, because
    the function leaves the brush as NoBrush and the mark fills its inner head
    immediately afterwards.
    """

    painter.save()
    pen = QPen(ink)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.restore()


def render_icon(size: int) -> bytes:
    """Render one square app icon and return its encoded PNG bytes.

    The mark is a download arrowhead with a smaller arrowhead nested inside it:
    the same glyph twice, at two scales, like a frame inside a frame.

    The nesting is the point.  Every downloader on the machine already owns a
    lone triangle pointing down, and a lone triangle is the single most
    borrowed shape in the category - it is why the previous version of this icon
    read as a scam badge.  Drawing the glyph at two scales is what makes it
    ownable without becoming something so abstract it stops meaning *download*.

    Only the outer head is stroked and only the inner one is filled, because two
    filled heads nested inside each other are just the larger of the two: the
    smaller one has to be the paper left between them, and only a stroke leaves
    paper there.

    Every corner in the mark is a curve - the arrowhead's three corners are
    rounded by a quadratic tangent to both of its edges, and the outline's
    joins are round - so the icon has no sharp corner anywhere.

    The outline is 0.082 of the canvas.  That is the weight the mark was drawn
    and approved at, and it is also what keeps the two heads clearly separate
    from 24 pixels up.  Its cost is known and accepted rather than fixed: 0.082
    is 1.2 pixels at 16, which is less than one pixel of solid white, so at taskbar
    size the mark has no fully-covered core and breaks into a few sub-pixel
    fragments.  It still reads there as a faint outlined arrowhead.  A bolder
    outline at 16 pixels is one number away - raise it and narrow the inner head
    to keep the same clearance - but that is a bolder mark than the one that was
    chosen, so it is not substituted silently.
    """

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        inset = size * 0.045
        span = size - inset * 2

        # The tile.  Square and full-bleed apart from a small margin, because
        # the taskbar and Alt-Tab both expect roughly square art.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(TILE)))
        painter.drawRoundedRect(
            QRectF(inset, inset, span, span), span * 0.235, span * 0.235
        )

        white = QColor(INK)
        painter.save()
        painter.translate(inset, inset)
        painter.scale(span, span)
        # Zoom the mark 8% about its centre.  At the coordinates below it spans
        # about 63% of the canvas, and at 256 pixels anything smaller reads as an
        # afterthought floating in the middle of the tile.
        painter.translate(0.5, 0.5)
        painter.scale(1.08, 1.08)
        painter.translate(-0.5, -0.5)

        # The outer head: the main component, and the one that has to hold the
        # shape at 16 pixels.  The 0.082 outline is the weight the mark was
        # approved at; it is also the heaviest outline that still leaves the
        # inner head visibly separate rather than fused into a single arrow.
        _stroke(
            painter,
            _arrow_head((0.500, 0.805), (0.0, 1.0), 0.500, 0.585, 0.075),
            white,
            0.082,
        )
        # The inner head.  At 0.265 wide the clearance between the two heads is
        # about 0.028 of the canvas at its tightest, and that tightest point is
        # the inner head's base corners rather than its tip.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(white))
        painter.drawPath(_arrow_head((0.500, 0.695), (0.0, 1.0), 0.235, 0.265, 0.050))
        painter.restore()
    finally:
        painter.end()

    # The QByteArray must outlive the QBuffer, so it is kept as a named local.
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise RuntimeError("Qt could not encode the icon as PNG.")
    buffer.close()
    return bytes(storage)


def write_pngs(directory: Path, sizes: tuple[int, ...] = PNG_SIZES) -> list[Path]:
    """Write one PNG per size into ``directory`` and return the paths written."""

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size in sizes:
        target = directory / f"app-{size}.png"
        target.write_bytes(render_icon(size))
        written.append(target)
    return written


def write_ico(path: Path, sizes: tuple[int, ...] = ICO_SIZES) -> None:
    """Write a Windows .ico file containing one PNG entry per size."""

    images = [(size, render_icon(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    directory = b""
    offset = len(header) + 16 * len(images)
    for size, payload in images:
        # A dimension byte of 0 means 256 pixels.
        encoded_dimension = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII",
            encoded_dimension,
            encoded_dimension,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        )
        offset += len(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + directory + b"".join(payload for _size, payload in images))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the application icon.")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "assets" / "app.ico"),
        help="Destination .ico path.",
    )
    parser.add_argument(
        "--png-dir",
        default=None,
        help="Where to write the standalone PNG ladder. Defaults to the folder "
        "holding the .ico; pass 'none' to skip.",
    )
    arguments = parser.parse_args()
    # A QGuiApplication must stay alive while QPixmap objects are used.
    app = QGuiApplication.instance() or QGuiApplication([])
    try:
        destination = Path(arguments.output)
        write_ico(destination, ICO_SIZES)
        print(f"Wrote {destination} ({destination.stat().st_size} bytes)")

        png_dir_arg = arguments.png_dir
        if png_dir_arg and png_dir_arg.strip().lower() == "none":
            png_dir = None
        else:
            png_dir = Path(png_dir_arg) if png_dir_arg else destination.parent
        if png_dir is not None:
            written = write_pngs(png_dir, PNG_SIZES)
            if written:
                print(f"Wrote {len(written)} PNGs in {png_dir}")
    finally:
        del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())