"""Stylesheet for the library: generated posters and episode rows.

Torrents come without artwork, so each title gets a typographic poster — its
name on a gradient picked stably from the title — which is enough to make a
shelf of titles scannable at a glance.
"""

import colorsys
import zlib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

# Posters take one of this many hues around the colour wheel. Fine enough
# that a shelf rarely shows two alike, coarse enough to be a small stylesheet.
_HUES = 36


def _hex(h: float, lightness: float, saturation: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, lightness, saturation)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _gradient_rules() -> str:
    """One gradient class per hue, deep enough everywhere to carry white text."""
    rules = []
    for i in range(_HUES):
        h = i / _HUES
        top, bottom = _hex(h, 0.42, 0.55), _hex((h + 0.04) % 1.0, 0.24, 0.6)
        rules.append(
            f".nab-poster-{i} {{ background: linear-gradient(160deg, {top}, {bottom}); }}"
        )
    return "\n".join(rules)


_CSS = """
.nab-poster {
    border-radius: 12px;
    color: white;
    box-shadow: 0 1px 3px alpha(black, 0.25), 0 2px 8px alpha(black, 0.12);
}
.nab-poster-inner { padding: 14px; }
.nab-poster-title {
    font-weight: 800;
    font-size: 15pt;
    text-shadow: 0 1px 2px alpha(black, 0.3);
}
.nab-poster.small .nab-poster-inner { padding: 0; }
.nab-poster-meta { font-size: 9pt; font-weight: 600; opacity: 0.8; }
/* Adwaita gives a trough a 150px minimum, wider than a poster's text column. */
progressbar.nab-progress trough { min-width: 0; }
.nab-poster progressbar trough {
    min-height: 4px;
    border-radius: 999px;
    background: alpha(white, 0.25);
}
.nab-poster progressbar progress {
    min-height: 4px;
    border-radius: 999px;
    background: white;
}
.nab-poster-badge {
    background: alpha(black, 0.3);
    border-radius: 999px;
    padding: 4px;
    margin: 10px;
}

flowboxchild.nab-card {
    padding: 6px;
    border-radius: 16px;
}
flowboxchild.nab-card:hover .nab-poster { filter: brightness(1.1); }
flowboxchild.nab-card:active .nab-poster { filter: brightness(0.95); }

.nab-episode-number { min-width: 2.2em; }
row.nab-watched .title { opacity: 0.55; }
.nab-up-next {
    font-size: 8pt;
    font-weight: 800;
    padding: 2px 8px;
    border-radius: 999px;
    color: var(--accent-fg-color);
    background: var(--accent-bg-color);
}
"""

_installed = False


def install() -> None:
    """Register the library stylesheet with the default display, once."""
    global _installed
    if _installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS + _gradient_rules())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _installed = True


def poster_class(key: str) -> str:
    """The gradient class for a title, stable across runs."""
    return f"nab-poster-{zlib.crc32(key.encode()) % _HUES}"
