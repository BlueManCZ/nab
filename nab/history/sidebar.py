"""Sidebar widget listing watch history.

Owns the history list view and its rendering; the window supplies the
database and an activation callback. Keeps GTK out of the data-layer modules
(``database``/``model``), mirroring ``subtitles.panel`` and ``series.nav``.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from nab.history.database import HistoryDatabase  # noqa: E402
from nab.history.model import HistoryEntry  # noqa: E402

log = logging.getLogger(__name__)

# Below this watched fraction, don't clutter the row with a progress badge.
_MIN_VISIBLE_PROGRESS = 0.01


def _humanize_timestamp(ts: str) -> str:
    # SQLite gives 'YYYY-MM-DD HH:MM:SS' in UTC.
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return ts
    delta = datetime.now(UTC) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class HistorySidebar:
    """Owns the history list widget and renders entries from the database."""

    def __init__(
        self,
        history: HistoryDatabase,
        *,
        on_activate: Callable[[str], None],
    ) -> None:
        self._history = history
        self._on_activate = on_activate
        self._build_widget()

    def _build_widget(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title="History"))
        toolbar.add_top_bar(header)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(self._list)
        scrolled.set_vexpand(True)
        toolbar.set_content(scrolled)
        self._widget = toolbar

    @property
    def widget(self) -> Gtk.Widget:
        """The sidebar widget to hand to the split view."""
        return self._widget

    def refresh(self) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt

        entries = self._history.list_recent(limit=100)
        if not entries:
            self._list.append(self._empty_row())
            return
        for entry in entries:
            self._list.append(self._build_row(entry))

    def _empty_row(self) -> Gtk.Widget:
        empty = Gtk.Label(label="Nothing nabbed yet.")
        empty.set_margin_top(24)
        empty.set_margin_bottom(24)
        empty.add_css_class("dim-label")
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        row.set_child(empty)
        return row

    def _build_row(self, entry: HistoryEntry) -> Gtk.Widget:
        row = Gtk.ListBoxRow()
        row.entry = entry  # attach for activation handler

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        title = entry.title or entry.input
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.add_css_class("heading")
        box.append(title_lbl)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badge = Gtk.Label(label=entry.source_type.replace("_", " "))
        badge.add_css_class("caption")
        badge.add_css_class("dim-label")
        meta_box.append(badge)

        when = Gtk.Label(label=_humanize_timestamp(entry.last_played_at))
        when.add_css_class("caption")
        when.add_css_class("dim-label")
        meta_box.append(when)

        if entry.duration and entry.duration > 0:
            progress = min(entry.last_position / entry.duration, 1.0) if entry.last_position else 0.0
            if progress > _MIN_VISIBLE_PROGRESS:
                pct = Gtk.Label(label=f"{int(progress * 100)}%")
                pct.add_css_class("caption")
                pct.add_css_class("dim-label")
                meta_box.append(pct)

        box.append(meta_box)
        row.set_child(box)
        return row

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        entry: HistoryEntry | None = getattr(row, "entry", None)
        if entry is not None:
            self._on_activate(entry.input)
