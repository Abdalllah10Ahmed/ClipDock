"""Every theme has to keep its text readable, in every mode.

A theme is a set of roles, so one stylesheet serves all of them and a single
wrong value makes a control unreadable.  These tests check the role pairs the
stylesheet actually paints, compositing translucent colors over whatever sits
behind them, because that is what Qt does when it draws.

Every palette is opaque, and ``gui.themes`` refuses to build one that is not, so
there is no desktop showing through anything here.  A role is still composited
over the surface beneath it rather than compared raw, because a palette is
allowed to use translucency *within* the window and a wrong alpha would be just
as unreadable as a wrong hue.
"""

from __future__ import annotations

import re
import unittest

from youtube_downloader.gui.themes import (
    DEFAULT_THEME,
    THEMES,
    THEME_IDS,
    is_dark,
    theme_palette,
)

# WCAG 1.4.3 for text, and 1.4.11 relaxed to a visible-but-quiet rule for
# borders: a control fill already differs from its panel, so a border only has
# to be perceptible, not high contrast.
TEXT_RATIO = 4.5
DISABLED_RATIO = 3.0
BORDER_RATIO = 1.3

# The pairs the stylesheet really paints, as (foreground, background, minimum).
# Text is checked against the card rather than the window: the stylesheet puts
# the whole app on a card, and the card is what every glyph is painted on.
ROLE_PAIRS = (
    ("text", "panel", TEXT_RATIO),
    ("text", "surface", TEXT_RATIO),
    ("text", "input", TEXT_RATIO),
    ("text", "button", TEXT_RATIO),
    ("text", "button_hover", TEXT_RATIO),
    ("muted", "panel", TEXT_RATIO),
    ("muted", "surface", TEXT_RATIO),
    ("muted", "input", TEXT_RATIO),
    ("primary_text", "primary", TEXT_RATIO),
    ("primary_text", "primary_hover", TEXT_RATIO),
    ("progress_text", "progress", TEXT_RATIO),
    ("progress_text", "track", TEXT_RATIO),
    ("tooltip_text", "tooltip", TEXT_RATIO),
    ("disabled_text", "disabled_bg", DISABLED_RATIO),
    ("border", "panel", BORDER_RATIO),
    ("border", "surface", BORDER_RATIO),
    ("border", "input", BORDER_RATIO),
)

# A fill a role is drawn on top of.  The card sits on the window, panels sit on
# the card, and inputs sit inside a panel, so a translucent input has to be
# checked against the panel rather than against the window behind both.  A
# tooltip is its own top-level window, so it composites over the window only.
_BEHIND = {
    "window": None,
    "panel": "window",
    "surface": "panel",
    "input": "surface",
    "button": "panel",
    "button_hover": "panel",
    "disabled_bg": "panel",
    "track": "panel",
    "tooltip": "window",
    "progress": "panel",
    "primary": "panel",
    "primary_hover": "panel",
}

_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")
_HEX = re.compile(r"#([0-9a-fA-F]{6})$")


def _parse(color: str) -> tuple[int, int, int, float] | None:
    """Split a theme color into channels plus alpha, or None for a gradient."""

    color = color.strip()
    match = _RGB.fullmatch(color)
    if match:
        r, g, b = (int(match.group(index)) for index in (1, 2, 3))
        alpha = float(match.group(4)) if match.group(4) is not None else 1.0
        # Qt accepts both a 0-1 and a 0-255 alpha channel.
        if alpha > 1.0:
            alpha /= 255.0
        return r, g, b, alpha
    match = _HEX.fullmatch(color)
    if match:
        value = int(match.group(1), 16)
        return (value >> 16) & 255, (value >> 8) & 255, value & 255, 1.0
    return None


def _over(fg: tuple[int, int, int, float], bg: tuple[int, int, int, float]) -> tuple[int, int, int]:
    alpha = fg[3] + bg[3] * (1 - fg[3])
    if alpha <= 0:
        return (0, 0, 0)
    return tuple(
        int(round((fg[index] * fg[3] + bg[index] * bg[3] * (1 - fg[3])) / alpha))
        for index in range(3)
    )


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(value) for value in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    a, b = _luminance(first), _luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _flattened(
    palette: dict[str, str],
    role: str,
) -> tuple[int, int, int] | None:
    """The color Qt actually paints for a role, with translucency resolved."""

    parsed = _parse(palette[role])
    if parsed is None:
        # A gradient fill: use the average of its stops, which is the worst case
        # for contrast only in the sense of being a fair middle sample.
        stops = [_parse(stop) for stop in re.findall(r"#[0-9a-fA-F]{6}", palette[role])]
        if not stops:
            return None
        return tuple(int(sum(stop[index] for stop in stops) / len(stops)) for index in range(3))
    parent = _BEHIND.get(role)
    if parent is None:
        return parsed[:3]
    base = _flattened(palette, parent)
    if base is None:
        return parsed[:3]
    return _over(parsed, (*base, 1.0))


class ThemeContrastTests(unittest.TestCase):
    def test_every_theme_role_pair_is_readable(self) -> None:
        for theme_id, label, _description in THEMES:
            palette = theme_palette(theme_id)
            for foreground, background, minimum in ROLE_PAIRS:
                with self.subTest(theme=label, pair=f"{foreground}/{background}"):
                    fg = _flattened(palette, foreground)
                    bg = _flattened(palette, background)
                    self.assertIsNotNone(fg, f"{label} has no {foreground} color")
                    self.assertIsNotNone(bg, f"{label} has no {background} color")
                    value = _contrast(fg, bg)
                    self.assertGreaterEqual(
                        value,
                        minimum,
                        f"{label}: {foreground} on {background} is {value:.2f}:1",
                    )

    def test_every_theme_card_is_opaque(self) -> None:
        # The card is the only thing text is ever painted on.  A palette that
        # let a translucent color in here would put the interface on whatever is
        # behind the window, which is the failure the removed see-through themes
        # were built to guard against.  gui.themes also refuses to build such a
        # palette; this asserts the guarantee holds for the palettes that exist.
        for theme_id, label, _description in THEMES:
            palette = theme_palette(theme_id)
            with self.subTest(theme=label):
                card = _parse(palette["panel"])
                self.assertIsNotNone(card, f"{label} has no card color")
                self.assertEqual(card[3], 1.0, f"{label}: the card is not opaque")

    def test_theme_catalogue_offers_the_requested_palettes(self) -> None:
        # The themes a user is most likely to look for by name.  The VS Code
        # entries are deliberately absent: Dark now *is* VS Code Dark+, and
        # VS Code Light+ was dropped as a near-duplicate of Light.
        for theme_id in (
            "light",
            "dark",
            "dracula",
            "nord",
            "tokyo_night",
            "one_dark",
            "catppuccin_mocha",
            "monokai",
            "gruvbox",
            "solarized_light",
        ):
            self.assertIn(theme_id, THEME_IDS)
        for theme_id in ("vscode_dark", "vscode_light"):
            self.assertNotIn(theme_id, THEME_IDS)
        # Every theme needs a label for the selector and a description for its
        # tooltip, or the control silently shows blank rows.
        for theme_id, label, description in THEMES:
            with self.subTest(theme=theme_id):
                self.assertTrue(label.strip())
                self.assertTrue(description.strip())
        self.assertEqual(len(THEMES), len(THEME_IDS))
        self.assertEqual(DEFAULT_THEME, THEMES[0][0])

    def test_dark_replaced_the_vanilla_neutral_palette(self) -> None:
        # Dark is now VS Code Dark+, so its own colors have to be the editor's
        # rather than the neutral slate the theme used to be built on.
        palette = theme_palette("dark")
        self.assertEqual(palette["window"], "#1f1f1f")
        self.assertEqual(palette["surface"], "#252526")
        self.assertEqual(palette["primary"], "#0078d4")
        self.assertNotEqual(palette["window"], "#0b1220")

    def test_unknown_theme_falls_back_instead_of_failing(self) -> None:
        self.assertEqual(theme_palette("no-such-theme"), theme_palette(DEFAULT_THEME))
        self.assertFalse(is_dark("no-such-theme"))

    def test_dark_and_light_classifications_match_the_windows(self) -> None:
        for theme_id, _label, _description in THEMES:
            with self.subTest(theme=theme_id):
                # is_dark exists to hint native widgets, and the only color
                # those ever see is the card the app is drawn on.
                card = _flattened(theme_palette(theme_id), "panel")
                self.assertIsNotNone(card)
                self.assertEqual(is_dark(theme_id), _luminance(card) < 0.5)

    def test_the_see_through_themes_are_gone(self) -> None:
        # Both asked the compositor for a window the desktop could show through,
        # and neither read well enough to keep.  Asserted by id so they cannot
        # quietly return in a reworded form.
        for theme_id in ("glass", "transparent"):
            with self.subTest(theme=theme_id):
                self.assertNotIn(theme_id, THEME_IDS)
        for _theme_id, label, _description in THEMES:
            self.assertNotIn(label.strip().lower(), {"glass", "transparent"})


if __name__ == "__main__":
    unittest.main()
