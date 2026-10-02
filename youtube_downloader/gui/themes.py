"""Color themes for the application window.

Every theme is the same set of roles, so :meth:`MainWindow._stylesheet` can be
written once.  Dark palettes are derived from :data:`_DARK_BASE` and light ones
from :data:`_LIGHT_BASE` so a theme only has to state the colors that actually
differ; the remaining roles keep sensible contrast for that family.

Every palette is fully opaque.  Two see-through themes used to live here -
``glass``, which asked the compositor for a frosted acrylic backdrop, and
``transparent``, which asked for no blur at all.  Neither read well enough to
keep, and without them the window no longer has to be a layered, frameless
compositor surface, so that machinery went with them.
"""

from __future__ import annotations

from typing import NamedTuple


class Theme(NamedTuple):
    """One selectable theme."""

    theme_id: str
    label: str
    description: str
    palette: dict[str, str]
    is_dark: bool


# Keys every palette must provide.  Listed explicitly so a typo in a theme
# definition fails loudly instead of rendering a half-themed control.
_REQUIRED_KEYS = (
    "background",
    "window",
    "panel",
    "surface",
    "input",
    "text",
    "muted",
    "border",
    "button",
    "button_hover",
    "primary",
    "primary_hover",
    "primary_text",
    "progress",
    "progress_text",
    "disabled_bg",
    "disabled_text",
    "track",
    "tooltip",
    "tooltip_text",
)

# ``window`` is what is behind everything, and ``panel`` is the card the whole
# app is drawn on.  With every palette opaque the two are always the same
# color, but they stay separate roles: the card is what text is painted on, and
# keeping it explicit means the stylesheet never has to reason about which of
# the two it is filling.  Neither base states ``panel`` - :func:`_palette`
# derives it from the final window color.
_LIGHT_BASE = {
    "background": "#f3f6fb",
    "window": "#f3f6fb",
    "surface": "#ffffff",
    "input": "#ffffff",
    "text": "#172033",
    "muted": "#526174",
    "border": "#c5d1e1",
    "button": "#e8eef7",
    "button_hover": "#dbe7f7",
    "primary": "#1d4ed8",
    "primary_hover": "#1e40af",
    "primary_text": "#ffffff",
    "progress": "#547be8",
    "progress_text": "#14181f",
    "disabled_bg": "#dbe3ee",
    "disabled_text": "#3f4a5a",
    "track": "#dbe4f0",
    "tooltip": "#172033",
    "tooltip_text": "#ffffff",
}

# Dark themes need the same pair of guarantees.  The progress bar prints its
# percentage on top of the filled chunk, so the label has to be readable
# against both the chunk and the empty track.  That is only possible when the
# two are close in lightness, which is why ``progress`` is a mid tone in every
# theme rather than the brightest accent color.
_DARK_BASE = {
    "background": "#0b1220",
    "window": "#0b1220",
    "surface": "#182235",
    "input": "#111827",
    "text": "#f8fafc",
    "muted": "#cbd5e1",
    "border": "#52627a",
    "button": "#2b3b55",
    "button_hover": "#3b506f",
    "primary": "#38bdf8",
    "primary_hover": "#0ea5e9",
    "primary_text": "#082f49",
    "progress": "#2563eb",
    "progress_text": "#ffffff",
    "disabled_bg": "#334155",
    "disabled_text": "#d3dce8",
    "track": "#334155",
    "tooltip": "#f8fafc",
    "tooltip_text": "#0f172a",
}


def _palette(base: dict[str, str], **overrides: str) -> dict[str, str]:
    palette = dict(base)
    palette.update(overrides)
    # background and the card both follow window unless a theme supplies a
    # richer fill, so a theme that only recolors the window gets matching
    # surfaces without having to say so.  The defaults are applied before the
    # key check, so a theme that means to differ can and does.
    palette.setdefault("background", palette["window"])
    palette.setdefault("panel", palette["window"])
    missing = [key for key in _REQUIRED_KEYS if key not in palette]
    if missing:  # pragma: no cover - guards a typo in a future theme
        raise ValueError(f"theme palette is missing keys: {', '.join(missing)}")
    if not _is_opaque_color(palette["panel"]):  # pragma: no cover - same reason
        raise ValueError(
            f"theme panel {palette['panel']!r} is translucent, but every theme is "
            "opaque: text would be painted on whatever is behind the window"
        )
    return palette


def _is_opaque_color(color: str) -> bool:
    """Whether a CSS color is fully opaque.

    A palette that slipped a translucent color in would put text on the desktop,
    so this is checked where the palette is built rather than only in a test.
    """

    value = color.strip().lower()
    if value.startswith("rgba"):
        parts = value[value.index("(") + 1 : value.rindex(")")].split(",")
        if len(parts) == 4:
            try:
                return float(parts[3]) >= 1.0
            except ValueError:
                return False
        return False
    if value.startswith("#") and len(value) == 9:  # #rrggbbaa
        return int(value[7:9], 16) >= 0xFF
    return True  # an unknown but named color is treated as opaque


_THEME_LIST: tuple[Theme, ...] = (
    Theme(
        theme_id="light",
        label="Light",
        description="Neutral light theme for daytime use.",
        palette=_palette(_LIGHT_BASE),
        is_dark=False,
    ),
    Theme(
        theme_id="dark",
        label="Dark",
        description="VS Code Dark+ colors: the familiar editor dark scheme.",
        palette=_palette(
            _DARK_BASE,
            background="#1f1f1f",
            window="#1f1f1f",
            surface="#252526",
            input="#1f1f1f",
            text="#cccccc",
            muted="#9d9d9d",
            border="#3c3c3c",
            button="#313131",
            button_hover="#3c3c3c",
            primary="#0078d4",
            primary_hover="#026ec1",
            primary_text="#ffffff",
            progress="#0078d4",
            disabled_bg="#3c3c3c",
            disabled_text="#8c8c8c",
            track="#3c3c3c",
            tooltip="#202020",
            tooltip_text="#cccccc",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="dracula",
        label="Dracula",
        description="The Dracula palette: deep charcoal with purple accents.",
        palette=_palette(
            _DARK_BASE,
            background="#282a36",
            window="#282a36",
            surface="#21222c",
            input="#1e1f29",
            text="#f8f8f2",
            muted="#9aa5ce",
            border="#44475a",
            button="#343746",
            button_hover="#44475a",
            primary="#bd93f9",
            primary_hover="#ff79c6",
            primary_text="#1e1f29",
            progress="#e40083",
            disabled_bg="#3a3c4b",
            disabled_text="#9aa0bd",
            track="#44475a",
            tooltip="#44475a",
            tooltip_text="#f8f8f2",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="nord",
        label="Nord",
        description="The Nord palette: cool arctic blues on slate.",
        palette=_palette(
            _DARK_BASE,
            background="#2e3440",
            window="#2e3440",
            surface="#3b4252",
            input="#2b303b",
            text="#eceff4",
            muted="#b8c5d6",
            border="#4c566a",
            button="#434c5e",
            button_hover="#4c566a",
            primary="#88c0d0",
            primary_hover="#8fbcbb",
            primary_text="#2e3440",
            progress="#5579a5",
            disabled_bg="#41495a",
            disabled_text="#93a1b3",
            track="#4c566a",
            tooltip="#4c566a",
            tooltip_text="#eceff4",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="tokyo_night",
        label="Tokyo Night",
        description="The Tokyo Night palette: deep indigo with bright blue accents.",
        palette=_palette(
            _DARK_BASE,
            background="#1a1b26",
            window="#1a1b26",
            surface="#1f2335",
            input="#16161e",
            text="#c0caf5",
            muted="#a3aed0",
            border="#3b4261",
            button="#24283b",
            button_hover="#2f334d",
            primary="#7aa2f7",
            primary_hover="#6d95ef",
            primary_text="#1a1b26",
            progress="#0079bf",
            disabled_bg="#2a2f45",
            disabled_text="#7b84a8",
            track="#2f344a",
            tooltip="#2a2f45",
            tooltip_text="#c0caf5",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="one_dark",
        label="One Dark",
        description="The Atom One Dark palette: muted charcoal with soft green.",
        palette=_palette(
            _DARK_BASE,
            background="#282c34",
            window="#282c34",
            surface="#2c313a",
            input="#21252b",
            text="#abb2bf",
            muted="#9aa4b2",
            border="#3e4451",
            button="#333842",
            button_hover="#3c414d",
            primary="#61afef",
            primary_hover="#4d9eea",
            primary_text="#1b1e24",
            progress="#32808a",
            disabled_bg="#353a44",
            disabled_text="#7c8697",
            track="#3a3f4a",
            tooltip="#3a3f4a",
            tooltip_text="#abb2bf",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="catppuccin_mocha",
        label="Catppuccin Mocha",
        description="The Catppuccin Mocha palette: pastel accents on a soft dark base.",
        palette=_palette(
            _DARK_BASE,
            background="#1e1e2e",
            window="#1e1e2e",
            surface="#232436",
            input="#181825",
            text="#cdd6f4",
            muted="#a6adc8",
            border="#45475a",
            button="#313244",
            button_hover="#45475a",
            primary="#89b4fa",
            primary_hover="#a6c8ff",
            primary_text="#1e1e2e",
            progress="#177dab",
            disabled_bg="#313244",
            disabled_text="#7f849c",
            track="#45475a",
            tooltip="#45475a",
            tooltip_text="#cdd6f4",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="monokai",
        label="Monokai",
        description="The Monokai palette: olive-black with acid green highlights.",
        palette=_palette(
            _DARK_BASE,
            background="#272822",
            window="#272822",
            surface="#2f3029",
            input="#1e1f1a",
            text="#f8f8f2",
            muted="#c5c8c6",
            border="#49483e",
            button="#3e3d32",
            button_hover="#49483e",
            primary="#a6e22e",
            primary_hover="#b5e35a",
            primary_text="#1e1f1a",
            progress="#e60657",
            disabled_bg="#3a3b33",
            disabled_text="#8f8d7f",
            track="#49483e",
            tooltip="#49483e",
            tooltip_text="#f8f8f2",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="gruvbox",
        label="Gruvbox Dark",
        description="The Gruvbox dark palette: warm retro earth tones.",
        palette=_palette(
            _DARK_BASE,
            background="#282828",
            window="#282828",
            surface="#32302f",
            input="#1d2021",
            text="#ebdbb2",
            muted="#bdae93",
            border="#504945",
            button="#3c3836",
            button_hover="#504945",
            primary="#fabd2f",
            primary_hover="#f8c75c",
            primary_text="#282828",
            progress="#5a7c6f",
            disabled_bg="#3c3836",
            disabled_text="#a89984",
            track="#504945",
            tooltip="#504945",
            tooltip_text="#ebdbb2",
        ),
        is_dark=True,
    ),
    Theme(
        theme_id="solarized_light",
        label="Solarized Light",
        description="The Solarized light palette: warm paper tones with teal accents.",
        palette=_palette(
            _LIGHT_BASE,
            background="#fdf6e3",
            window="#fdf6e3",
            surface="#f5efdc",
            input="#fbf8ee",
            text="#073642",
            muted="#586e75",
            border="#93a1a1",
            button="#eee8d5",
            button_hover="#e2ddc8",
            # One step darker than the canonical Solarized blue: the stock value
            # is too light for a white button label.
            primary="#217ab8",
            primary_hover="#1a6296",
            progress="#2aa198",
            disabled_bg="#eee8d5",
            disabled_text="#6b7a7a",
            track="#ddd6c1",
            tooltip="#eee8d5",
            tooltip_text="#073642",
        ),
        is_dark=False,
    ),
)

# Lookup tables used by the window.  THEMES is the ordered (id, label,
# description) triple the combo box renders, so the window never has to know
# anything about a palette to build the control.
THEMES: tuple[tuple[str, str, str], ...] = tuple(
    (theme.theme_id, theme.label, theme.description) for theme in _THEME_LIST
)

THEME_IDS: frozenset[str] = frozenset(theme.theme_id for theme in _THEME_LIST)
DEFAULT_THEME = _THEME_LIST[0].theme_id


def theme_palette(theme_id: str) -> dict[str, str]:
    """Return the color roles for a theme, falling back to the default."""

    for theme in _THEME_LIST:
        if theme.theme_id == theme_id:
            return theme.palette
    return _THEME_LIST[0].palette


def is_dark(theme_id: str) -> bool:
    """Whether a theme is a dark palette, used for native widget hints."""

    for theme in _THEME_LIST:
        if theme.theme_id == theme_id:
            return theme.is_dark
    return False
