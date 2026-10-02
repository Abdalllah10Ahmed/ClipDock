"""Combo boxes that behave themselves.

Every option list in the window is a ``SelectorComboBox`` rather than a plain
``QComboBox``.  Two problems with the stock widget are fixed here:

* The mouse wheel used to change the selected entry.  Nothing else on a wheel
  mouse or a trackpad does that, and on this window a stray scroll could change
  the download mode, the quality, or the theme with no click at all.
* There was no visible marker that a list opens at all, because the stylesheet
  removed the native arrow.  The chevron is painted here instead, so it can use
  the theme's own colours and stay visible on every background.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QComboBox

# The right-hand gutter the stylesheet reserves for the arrow (QComboBox's
# ::drop-down width).  The chevron is centred inside it so it lands exactly where
# the platform's own arrow used to.  The two must be changed together.
_ARROW_GUTTER = 26
_CHEVRON_WIDTH = 9
_CHEVRON_DROP = 5


class SelectorComboBox(QComboBox):
    """A drop-down list with a painted chevron that ignores the mouse wheel."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        # Until a theme is applied the chevron falls back to Qt's own text
        # colour, so it is never invisible if it is painted too early.
        self._chevron_color = QColor(self.palette().color(self.foregroundRole()))
        self._chevron_disabled_color = QColor(
            self.palette().color(self.foregroundRole())
        ).lighter(120)

    def set_chevron_colors(self, normal: str, disabled: str) -> None:
        """Take the chevron colours from the palette the theme just installed."""

        self._chevron_color = QColor(normal)
        self._chevron_disabled_color = QColor(disabled)
        self.update()

    def wheelEvent(self, event: Any) -> None:
        """Refuse to let the wheel move the selection.

        Ignored rather than consumed, so the scroll area behind the widget still
        scrolls: a cursor resting on a list is a normal thing to happen while
        reading the page, and swallowing the event outright would make the page
        appear stuck under the cursor.  The wheel is simply not this widget's
        to act on.
        """

        event.ignore()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            color = self._chevron_color if self.isEnabled() else self._chevron_disabled_color
            pen = QPen(color)
            pen.setWidthF(1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            tip_x = self.width() - _ARROW_GUTTER / 2
            top = (self.height() - _CHEVRON_DROP) / 2
            painter.drawPolyline(
                QPolygonF(
                    [
                        QPointF(tip_x - _CHEVRON_WIDTH / 2, top),
                        QPointF(tip_x, top + _CHEVRON_DROP),
                        QPointF(tip_x + _CHEVRON_WIDTH / 2, top),
                    ]
                )
            )
        finally:
            painter.end()