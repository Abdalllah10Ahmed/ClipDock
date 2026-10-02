"""The three window buttons, drawn the way Windows draws them.

A frameless window has to supply its own caption controls, and the first
attempt styled them as three small rounded buttons with a border and a surface
of their own. That made them read as part of the application's UI rather than as
the window frame, which is not what they are. These buttons instead follow the
platform:

* No border and no resting background. A caption button at rest is just a
  glyph on the title bar; a box drawn around it is the thing that made them look
  like typed text with a frame.
* The glyph is painted rather than being a character in the button's font. A
  text glyph is laid out and baseline-aligned by the font, so the three marks
  sit at three different heights and three different weights. Painted geometry
  puts all three on the same optical centre with the same stroke.
* Hover and press highlight the whole button, edge to edge, with no rounding.
  Windows caption buttons are rectangles, and a rounded one is immediately
  readable as a custom control.

The buttons stay flat and square on purpose. What makes a window frame legible
is that it is unchanged in every application, so anything decorative here is a
cost.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QPushButton

# Glyph identities. Named rather than passed as characters, because the drawn
# shape is the point and a character would reintroduce the font.
MINIMIZE = "minimize"
MAXIMIZE = "maximize"
RESTORE = "restore"
CLOSE = "close"

# Windows' own accent for the close button's hover, rather than a red picked to
# suit a theme. A caption button that changes colour with the application's
# palette stops being recognisable as the close button.  The stylesheet paints
# the background from these and the glyph turns white on top of it, so they live
# here rather than being repeated in both places.
CLOSE_HOVER = "#c42b1c"
CLOSE_HOVER_PRESSED = "#b1271b"
ON_CLOSE_HOVER = "#ffffff"

# Stroke weight for the painted glyphs, in logical pixels.  The shell draws its
# own at about 1px at 100% scaling.  1.4 rather than a flat 1 because a 1px pen
# straddles two device pixels at any display above 100% and comes out as two
# half-covered ones, which is a grey ghost rather than a line.  This is the
# weight that stays one solid mark at 125% and 150% without looking heavy at
# 100%.
_STROKE = 1.4

# Half the width of the maximize square and the length of the minimize line.
_HALF_GLYPH = 5.0


class CaptionButton(QPushButton):
    """A window caption button: a flat rectangle with a painted glyph."""

    def __init__(self, glyph: str, tooltip: str, parent: Any = None) -> None:
        # No text at all. The base class lays text out against the font's
        # baseline, which is exactly the misregistration being avoided here.
        super().__init__(parent)
        self._glyph = glyph
        self._glyph_color = QColor("#202124")
        self._hover_glyph_color = QColor("#000000")
        self._pressed_glyph_color = QColor("#000000")
        self._surface_color = QColor("#ffffff")
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_glyph(self, glyph: str) -> None:
        """Swap the drawn shape, for the maximize/restore pair."""

        self._glyph = glyph
        self.update()

    def glyph(self) -> str:
        return self._glyph

    def set_caption_colors(
        self,
        glyph: str,
        hover_glyph: str,
        pressed_glyph: str,
        surface: str,
    ) -> None:
        """Take the drawing colours from the palette the theme just installed.

        ``surface`` is only used to knock the restore glyph's back square out
        from behind its front square; without it the two outlines cross and the
        mark reads as a scribble rather than as two windows.
        """

        self._glyph_color = QColor(glyph)
        self._hover_glyph_color = QColor(hover_glyph)
        self._pressed_glyph_color = QColor(pressed_glyph)
        self._surface_color = QColor(surface)
        self.update()

    def _is_close(self) -> bool:
        return self._glyph == CLOSE

    def paintEvent(self, event: Any) -> None:
        # The stylesheet owns the background: it is transparent at rest and a
        # flat highlight on hover or press. Only the glyph is drawn here.
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            hovered = self.underMouse()
            if hovered:
                color = self._pressed_glyph_color if self.isDown() else self._hover_glyph_color
                # The close button's hover and press backgrounds are dark, so the
                # glyph turns white on both. A themed glyph colour would be
                # unreadable against them.
                if self._is_close():
                    color = QColor(ON_CLOSE_HOVER)
            else:
                color = self._glyph_color
            pen = QPen(color)
            pen.setWidthF(_STROKE)
            # Round caps and joins match the shell's own drawing and stop the
            # minimize line and the close cross from looking chopped off.
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            centre_x = self.width() / 2.0
            centre_y = self.height() / 2.0
            if self._glyph == MINIMIZE:
                # Sits slightly below centre, which is where Windows puts it and
                # is what makes it read as a minimize mark rather than as a dash
                # floating in the middle of a button.
                y = centre_y + 3.0
                painter.drawLine(
                    QPointF(centre_x - _HALF_GLYPH, y), QPointF(centre_x + _HALF_GLYPH, y)
                )
            elif self._glyph == MAXIMIZE:
                self._draw_square(painter, centre_x - _HALF_GLYPH, centre_y - _HALF_GLYPH)
            elif self._glyph == RESTORE:
                self._draw_restore(painter, centre_x, centre_y)
            elif self._glyph == CLOSE:
                self._draw_close(painter, centre_x, centre_y)
        finally:
            painter.end()

    def _draw_square(self, painter: QPainter, left: float, top: float) -> None:
        painter.drawRect(QRectF(left, top, _HALF_GLYPH * 2, _HALF_GLYPH * 2))

    def _draw_restore(self, painter: QPainter, centre_x: float, centre_y: float) -> None:
        """Two overlapping squares, the back one clipped by the front one."""

        offset = 2.0
        back = QRectF(
            centre_x - _HALF_GLYPH,
            centre_y - _HALF_GLYPH,
            _HALF_GLYPH * 2,
            _HALF_GLYPH * 2,
        )
        front = back.translated(offset, offset)
        painter.drawRect(back)
        # Filled, not just outlined: the back square's lower and right edges run
        # through the front square, and only painting over that overlap leaves
        # the recognizable two-window mark instead of a crossed grid.
        painter.fillRect(front, self._surface_color)
        painter.drawRect(front)

    def _draw_close(self, painter: QPainter, centre_x: float, centre_y: float) -> None:
        span = _HALF_GLYPH - 0.5
        painter.drawLine(
            QPointF(centre_x - span, centre_y - span), QPointF(centre_x + span, centre_y + span)
        )
        painter.drawLine(
            QPointF(centre_x + span, centre_y - span), QPointF(centre_x - span, centre_y + span)
        )