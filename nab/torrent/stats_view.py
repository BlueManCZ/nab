"""Live torrent transfer stats, rendered as a small dashboard.

Shown on the now-playing card while a torrent streams. Holds no libtorrent
dependency — it just reads attributes off whatever status snapshot it's
handed (see ``TorrentStatus`` in :mod:`nab.torrent.engine`), so it imports
cleanly even when libtorrent isn't installed.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


def _format_size(num_bytes: float) -> str:
    n = max(0.0, float(num_bytes))
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n:.0f} B"


def _format_rate(bytes_per_sec: float) -> str:
    return f"{_format_size(bytes_per_sec)}/s"


def _stat_tile(icon_name: str, caption: str) -> tuple[Gtk.Widget, Gtk.Label]:
    """A vertical icon / value / caption tile. Returns ``(tile, value_label)``."""
    tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    tile.set_halign(Gtk.Align.CENTER)
    tile.set_hexpand(True)

    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_opacity(0.6)
    tile.append(icon)

    value = Gtk.Label(label="—")
    value.add_css_class("title-4")
    value.add_css_class("numeric")
    tile.append(value)

    cap = Gtk.Label(label=caption)
    cap.add_css_class("caption")
    cap.add_css_class("dim-label")
    tile.append(cap)

    return tile, value


class TorrentStatsView:
    """Progress bar + speed / swarm tiles, refreshed from a status snapshot."""

    def __init__(self) -> None:
        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        specs = [
            ("go-down-symbolic", "Download"),
            ("go-up-symbolic", "Upload"),
            ("system-users-symbolic", "Peers"),
            ("network-server-symbolic", "Seeds"),
        ]
        labels: list[Gtk.Label] = []
        for i, (icon_name, caption) in enumerate(specs):
            if i:
                sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                sep.set_margin_top(4)
                sep.set_margin_bottom(4)
                row.append(sep)
            tile, value = _stat_tile(icon_name, caption)
            row.append(tile)
            labels.append(value)
        self._down, self._up, self._peers, self._seeds = labels

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_margin_top(8)
        outer.append(self._progress)
        outer.append(row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(440)
        clamp.set_child(outer)
        self.widget: Gtk.Widget = clamp

        self.reset()

    def reset(self) -> None:
        """Blank the tiles back to placeholders (no transfer data yet)."""
        self._progress.set_fraction(0.0)
        self._progress.set_text("Buffering…")
        for label in (self._down, self._up, self._peers, self._seeds):
            label.set_text("—")

    def update(self, st) -> None:
        """Refresh from a ``TorrentStatus`` snapshot."""
        self._down.set_text(_format_rate(st.download_rate))
        self._up.set_text(_format_rate(st.upload_rate))
        self._peers.set_text(str(st.num_peers))
        self._seeds.set_text(str(st.num_seeds))
        if st.total_wanted > 0:
            frac = min(1.0, st.total_done / st.total_wanted)
            self._progress.set_fraction(frac)
            self._progress.set_text(f"{frac * 100:.0f}% of {_format_size(st.total_wanted)}")
        else:
            self._progress.set_fraction(0.0)
            self._progress.set_text("Buffering…")
