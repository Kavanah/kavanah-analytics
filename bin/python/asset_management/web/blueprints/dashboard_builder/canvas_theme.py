"""canvas theme tokens: the single source of truth.

A theme is a named token set — ``colors`` (page/surface/ink/accent/chart
series), ``fonts`` (family map + rich-text size whitelist + named size
scale), ``components`` (border/radius/spacing plus the stat-tile and header
variants) — that object ``style`` fields may reference BY KEY instead of
carrying raw hex/px values. This extends the ``cvTextTokens()`` rich-text
convention to object-level styling.

Resolution happens at exactly two shared points, one per language, both fed
from THIS module's data:

- Python: ``resolve_color`` / ``resolve_font_size`` / ``resolve_style`` here,
  used by ``canvas_seed.py`` (report seeds + the AI style constants).
- JS: ``canvas_theme.js`` resolvers on all three canvas surfaces (editor,
  iframe viewer, public viewer), reading ``window.CANVAS_THEME_TOKENS`` —
  injected by the templates from :func:`tokens_json`, so the JS side carries
  NO copy of the token data.

Back-compat contract: a raw value (``#1a2744``, ``15``) passes
through every resolver unchanged, so documents saved before theming render
byte-identically; only a value that exactly matches a token key resolves.
The ``classic`` theme IS the pre-theme look — every value below is the
literal the render sites carried before this module existed.
"""

from __future__ import annotations

import copy
import json

DEFAULT_THEME_KEY = "classic"

# The earlier look, verbatim. colors is a superset of the client's
# cvTextTokens() color set (rich-text runs keep resolving through that set;
# the release-plan test pins the subset relationship). fonts.family and
# fonts.sizes mirror cvTextTokens exactly; fonts.scale adds named steps for
# object-level font_size tokens. components.stat_tile carries the
# _AI_STAT_STYLE slots as COLOR TOKEN KEYS — resolve_style() turns it back
# into the legacy raw-hex dict.
_CLASSIC: dict = {
    "key": "classic",
    "name": "Classic",
    "colors": {
        "bg": "#ffffff",  # default page background
        "paper": "#f0ece4",  # impact-report page backdrop (.rpt parchment)
        "surface": "#ffffff",  # card / tile surface
        "ink": "#16191f",  # primary text (== cvTextTokens ink)
        "muted": "#5a6a80",  # secondary text (seed sub-headers)
        "accent": "#1a2744",  # report navy (headers, shape borders)
        "border": "#E9EBED",  # stat-tile border
        "value_ink": "#000716",  # stat value text
        "header_bg": "#2c6da0",  # label strip / table title bar
        "header_text": "#ffffff",
        "subheader_bg": "#5a9cc5",  # grouped_table group-header bar
        "shape_fill": "#dbe7f5",  # default shape fill
        # Impact-report header decorations — the on-accent tints
        # the .rpt-header renders: gold org eyebrow, white serif title,
        # muted subtitle, and the date-range pill.
        "eyebrow": "#e8c77b",
        "accent_ink": "#ffffff",
        "accent_soft": "#b9c2d4",
        "pill_bg": "#2b3a5e",
        "pill_ink": "#cfd6e4",
        # Accent used AS TEXT on a surface (funnel step values). Classic ==
        # accent (navy works as both); a dark theme must lighten it — its
        # accent doubles as a card color and would vanish on its surface.
        "accent_text": "#1a2744",
        # Rich-text palette (cvTextTokens colors — ink/white above).
        "slate": "#5f6b7a",
        "navy": "#1a2744",
        "blue": "#0972d3",
        "green": "#037f0c",
        "amber": "#8a6116",
        "red": "#d91515",
        "white": "#ffffff",
        # Chart series palette (the impact-report trend colors).
        "series": ["#2a8a7a", "#c9953c", "#d4654a", "#6b5ce7", "#1a2744"],
    },
    "fonts": {
        "family": {
            "sans": "-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
            "serif": "Georgia, 'Times New Roman', serif",
            "slab": "'Trebuchet MS', Verdana, sans-serif",
            "mono": "Consolas, 'Courier New', monospace",
        },
        # Rich-text run size whitelist (cvTextTokens sizes).
        "sizes": [12, 14, 15, 18, 20, 24, 28, 32, 40, 48],
        # Named scale for object-level font_size token keys. NOT yet
        # persistable (user decision 2026-07-17): the live index maps
        # style.font_size as long, so the save gate rejects string sizes
        # until a canvas_config dynamic:false reindex migration lands —
        # the resolvers below are the ready hook for that day.
        "scale": {"xs": 11, "sm": 12, "md": 15, "lg": 18, "xl": 20, "xxl": 26},
    },
    "components": {
        "border": {"width": 1},
        "radius": {"card": 14, "pill": 100},
        "spacing": {"gap": 16, "margin": 48},
        # Stat-tile variant — the proven grouped_table cell look. Color slots
        # are token keys into colors above.
        "stat_tile": {
            "variant": "table_cell",
            "align": "center",
            "bg_color": "surface",
            "border_color": "border",
            "label_position": "top",
            "label_bg_color": "header_bg",
            "label_color": "header_text",
            "label_font_size": 12,
            "label_bold": True,
            "value_color": "value_ink",
            "value_font_size": 18,
        },
        # Section/table header variant (grouped_table title bar).
        "header": {"color": "header_text", "bg_color": "header_bg"},
    },
}


def _theme_variant(key: str, name: str, colors: dict) -> dict:
    """A full theme built from the classic template with *colors* overridden.

    Fonts and component variants are shared with classic on purpose: the
    component dicts reference colors BY TOKEN KEY (``header_bg`` etc.), so a
    variant restyles them through its own palette without carrying a copy.
    Every variant therefore exposes exactly the classic token key set — the
     library contract the release-plan test pins.
    """
    theme = copy.deepcopy(_CLASSIC)
    theme["key"] = key
    theme["name"] = name
    theme["colors"] = {**theme["colors"], **colors}
    return theme


# ── the built-in theme library ───────────────────────────────────
# Curated, code-defined themes referenced by key (the icon-library precedent:
# a bundled set, no user-created entries — that's a future epic). ``classic``
# stays first and default; each variant is a complete standalone token set.

_MIDNIGHT = _theme_variant(
    "midnight",
    "Midnight",
    {
        "bg": "#10141c",
        "paper": "#161b26",
        "surface": "#1c2432",
        "ink": "#e8ecf4",
        "muted": "#9aa8bd",
        "accent": "#233350",
        "border": "#2e3a4e",
        "value_ink": "#f4f8ff",
        "header_bg": "#35619b",
        "header_text": "#ffffff",
        "subheader_bg": "#4a7aae",
        "shape_fill": "#24334b",
        "eyebrow": "#e3c06f",
        "accent_ink": "#ffffff",
        "accent_soft": "#9fb3d6",
        "pill_bg": "#31415f",
        "pill_ink": "#c9d4ea",
        "accent_text": "#c7d7f5",
        # Rich-text named colors brightened for dark surfaces — a run keyed
        # "navy" must stay readable when the page behind it goes dark.
        "slate": "#9aa8bb",
        "navy": "#a9c0e8",
        "blue": "#63a9ff",
        "green": "#4ccb7a",
        "amber": "#e5b95c",
        "red": "#ff7a70",
        "series": ["#4ecdb4", "#e2b45c", "#ef8a6e", "#9186f0", "#5f8fe8"],
    },
)

_EVERGREEN = _theme_variant(
    "evergreen",
    "Evergreen",
    {
        "bg": "#ffffff",
        "paper": "#eef3ec",
        "surface": "#ffffff",
        "ink": "#1d2823",
        "muted": "#5f7268",
        "accent": "#1f4d3a",
        "border": "#dbe6dc",
        "value_ink": "#0b241a",
        "header_bg": "#2e7d5b",
        "header_text": "#ffffff",
        "subheader_bg": "#5da588",
        "shape_fill": "#dcebe1",
        "eyebrow": "#dcc27b",
        "accent_ink": "#ffffff",
        "accent_soft": "#b5cec2",
        "pill_bg": "#2a5c47",
        "pill_ink": "#d2e7da",
        "accent_text": "#1f4d3a",
        "series": ["#2e7d5b", "#c9953c", "#b05d3f", "#4a6fa5", "#6d5aa8"],
    },
)

_TERRACOTTA = _theme_variant(
    "terracotta",
    "Terracotta",
    {
        "bg": "#fffcf7",
        "paper": "#f6efe3",
        "surface": "#ffffff",
        "ink": "#2b211a",
        "muted": "#82705f",
        "accent": "#7e3b28",
        "border": "#eadfd0",
        "value_ink": "#361a0f",
        "header_bg": "#b25b32",
        "header_text": "#ffffff",
        "subheader_bg": "#cd8757",
        "shape_fill": "#f4e4d2",
        "eyebrow": "#e8c77b",
        "accent_ink": "#fff4e9",
        "accent_soft": "#dfb6a3",
        "pill_bg": "#63301f",
        "pill_ink": "#f0d9c4",
        "accent_text": "#7e3b28",
        "series": ["#b25b32", "#8f7a3c", "#4f7d6b", "#7a5ca8", "#3f5f8a"],
    },
)

THEMES: dict[str, dict] = {
    DEFAULT_THEME_KEY: _CLASSIC,
    "midnight": _MIDNIGHT,
    "evergreen": _EVERGREEN,
    "terracotta": _TERRACOTTA,
}

# Object style fields whose string values may be color token keys. Keep in
# sync with COLOR_FIELDS in canvas_theme.js (the release-plan test pins the
# two lists against each other).
COLOR_STYLE_FIELDS = (
    "bg_color",
    "color",
    "border_color",
    "fill_color",
    "value_color",
    "label_color",
    "label_bg_color",
)
# Fields whose string values may be fonts.scale token keys (numbers pass
# through untouched).
FONT_SIZE_STYLE_FIELDS = ("font_size", "value_font_size", "label_font_size")

# Server-side bound for any token key (matches the rich-text run color/font
# bound in _validate_canvas_rich_text).
MAX_TOKEN_KEY_LEN = 40


def get_theme(ref: str | None = None) -> dict:
    """The theme for *ref* — unknown/missing refs fall back to ``classic``.

    The fallback (never an error) is load-bearing: a doc saved with a theme
    key a later deploy removed must still render.
    """
    if isinstance(ref, str) and ref in THEMES:
        return THEMES[ref]
    return THEMES[DEFAULT_THEME_KEY]


def resolve_color(value: object, theme: dict | None = None) -> object:
    """Token key -> concrete color; anything else passes through raw."""
    theme = theme or get_theme()
    colors = theme.get("colors") or {}
    if isinstance(value, str) and isinstance(colors.get(value), str):
        return colors[value]
    return value


def resolve_font_size(value: object, theme: dict | None = None) -> object:
    """fonts.scale key -> concrete px number; anything else passes through."""
    theme = theme or get_theme()
    scale = (theme.get("fonts") or {}).get("scale") or {}
    if isinstance(value, str) and isinstance(scale.get(value), (int, float)):
        return scale[value]
    return value


def resolve_style(style: dict, theme: dict | None = None) -> dict:
    """A copy of *style* with every token-keyed field resolved to its value.

    Non-dict input returns an empty dict (mirrors the renderers'
    ``obj.style || {}`` posture).
    """
    if not isinstance(style, dict):
        return {}
    theme = theme or get_theme()
    out = dict(style)
    for field in COLOR_STYLE_FIELDS:
        if field in out:
            out[field] = resolve_color(out[field], theme)
    for field in FONT_SIZE_STYLE_FIELDS:
        if field in out:
            out[field] = resolve_font_size(out[field], theme)
    return out


def series_colors(theme: dict | None = None) -> tuple[str, ...]:
    """The theme's chart-series palette as a tuple."""
    theme = theme or get_theme()
    return tuple((theme.get("colors") or {}).get("series") or ())


def stat_tile_style(theme: dict | None = None) -> dict:
    """The stat-tile component variant with its color tokens resolved."""
    theme = theme or get_theme()
    variant = (theme.get("components") or {}).get("stat_tile") or {}
    return resolve_style(copy.deepcopy(variant), theme)


def theme_summaries() -> list[dict]:
    """Picker-sized view of the library for ``/api/canvas-themes``:
    key + display name + the swatch colors a preview card needs. Full token
    sets stay template-injected (:func:`tokens_json`); the picker on the
    builder index has no canvas template, so it fetches this instead.
    """
    out = []
    for theme in THEMES.values():
        colors = theme.get("colors") or {}
        out.append(
            {
                "key": theme["key"],
                "name": theme["name"],
                "swatch": {
                    "bg": colors.get("bg"),
                    "paper": colors.get("paper"),
                    "surface": colors.get("surface"),
                    "ink": colors.get("ink"),
                    "accent": colors.get("accent"),
                    "header_bg": colors.get("header_bg"),
                    "series": list(colors.get("series") or []),
                },
            }
        )
    return out


def tokens_json() -> str:
    """The full theme map as JSON for the ``window.CANVAS_THEME_TOKENS``
    template injection (values are code-defined constants — no user data).
    """
    return json.dumps(THEMES, separators=(",", ":"))
