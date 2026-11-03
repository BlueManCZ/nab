"""The strips above the transport controls.

The now-playing strip says what's loading or playing — and, for a torrent, how
the download is going — wherever the user is in the library, so browsing never
hides it. The title is a button back to the show it belongs to; the live torrent
dashboard sits in a popover off a compact transfer readout.

The background-download strip stays independent of it: a torrent finishing its
queue after the stream ended is still using the network whatever else plays, so
it's always on show and can always be called off.
"""

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from nab.torrent.stats_view import (  # noqa: E402
    TorrentStatsView,
    format_rate,
    format_size,
)


class NowPlayingBar:
    def __init__(self, *, on_open: Callable[[], None]) -> None:
        self._title = Gtk.Label(xalign=0.0)
        self._title.set_ellipsize(Pango.EllipsizeMode.END)
        self._title.add_css_class("heading")
        self._status = Gtk.Label(xalign=0.0)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._status.add_css_class("caption")
        self._status.add_css_class("dim-label")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.append(self._title)
        text.append(self._status)

        self._spinner = Adw.Spinner()
        self._spinner.set_visible(False)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content.append(self._spinner)
        content.append(text)

        self._open_button = Gtk.Button(child=content)
        self._open_button.add_css_class("flat")
        self._open_button.set_hexpand(True)
        self._open_button.set_tooltip_text("Show in library")
        self._open_button.connect("clicked", lambda _b: on_open())

        self.torrent_stats = TorrentStatsView()
        self.torrent_stats.widget.set_margin_top(12)
        self.torrent_stats.widget.set_margin_bottom(12)
        self.torrent_stats.widget.set_margin_start(12)
        self.torrent_stats.widget.set_margin_end(12)
        self.torrent_stats.widget.set_size_request(380, -1)
        popover = Gtk.Popover(child=self.torrent_stats.widget)

        self._transfer = Gtk.MenuButton(popover=popover)
        self._transfer.set_always_show_arrow(True)
        self._transfer.add_css_class("flat")
        self._transfer.add_css_class("numeric")
        self._transfer.set_valign(Gtk.Align.CENTER)
        self._transfer.set_tooltip_text("Torrent transfer")
        self._transfer.set_visible(False)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(6)
        bar.set_margin_end(12)
        bar.set_margin_top(6)
        bar.append(self._open_button)
        bar.append(self._transfer)

        self.widget: Gtk.Widget = Gtk.Revealer(child=bar)
        self.widget.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)

    def show(self, title: str, status: str, *, busy: bool = False) -> None:
        self._title.set_text(title)
        self._title.set_tooltip_text(title)
        self._status.set_text(status)
        self._spinner.set_visible(busy)
        self.widget.set_reveal_child(True)

    def set_title(self, title: str) -> None:
        self._title.set_text(title)
        self._title.set_tooltip_text(title)

    def set_status(self, status: str, *, busy: bool = False) -> None:
        self._status.set_text(status)
        self._spinner.set_visible(busy)

    def set_openable(self, openable: bool) -> None:
        """Whether the title links to a page in the library."""
        self._open_button.set_sensitive(openable)

    def hide(self) -> None:
        self.stop_torrent()
        self.widget.set_reveal_child(False)

    def start_torrent(self) -> None:
        self.torrent_stats.reset()
        self._transfer.set_label("Buffering…")
        self._transfer.set_visible(True)

    def update_torrent(self, status) -> None:
        """Refresh from a ``TorrentStatus`` snapshot."""
        self.torrent_stats.update(status)
        label = f"↓ {format_rate(status.download_rate)}"
        if status.total_wanted > 0:
            done = min(1.0, status.total_done / status.total_wanted)
            if done < 1.0:
                label = f"{done * 100:.0f}% · {label}"
            else:
                label = "Downloaded"
        self._transfer.set_label(label)

    def stop_torrent(self) -> None:
        self._transfer.set_visible(False)
        self._transfer.popdown()


class BackgroundDownloadBar:
    def __init__(self, *, on_cancel: Callable[[], None]) -> None:
        self._title = Gtk.Label(xalign=0.0)
        self._title.set_ellipsize(Pango.EllipsizeMode.END)
        self._title.add_css_class("heading")
        self._status = Gtk.Label(xalign=0.0)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._status.add_css_class("caption")
        self._status.add_css_class("dim-label")
        self._status.add_css_class("numeric")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(self._title)
        text.append(self._status)

        cancel = Gtk.Button.new_from_icon_name("process-stop-symbolic")
        cancel.add_css_class("flat")
        cancel.set_valign(Gtk.Align.CENTER)
        cancel.set_tooltip_text("Stop downloading")
        cancel.connect("clicked", lambda _b: on_cancel())

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(6)
        bar.append(Gtk.Image.new_from_icon_name("folder-download-symbolic"))
        bar.append(text)
        bar.append(cancel)

        self.widget: Gtk.Widget = Gtk.Revealer(child=bar)
        self.widget.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)

    @property
    def shown(self) -> bool:
        return self.widget.get_reveal_child()

    def update(self, title: str, status) -> None:
        """Refresh from a ``BackgroundStatus`` snapshot and make sure it's on show."""
        label = f"Downloading {title}"
        self._title.set_text(label)
        self._title.set_tooltip_text(label)
        parts = [
            f"↓ {format_rate(status.download_rate)}",
            f"↑ {format_rate(status.upload_rate)}",
            f"{status.num_peers} peer{'' if status.num_peers == 1 else 's'}",
        ]
        if status.total_wanted > 0:
            done = min(1.0, status.total_done / status.total_wanted)
            parts.insert(0, f"{done * 100:.0f}% of {format_size(status.total_wanted)}")
        self._status.set_text(" · ".join(parts))
        self.widget.set_reveal_child(True)

    def hide(self) -> None:
        self.widget.set_reveal_child(False)
