"""Transport-bar widget + logic for navigating a detected series.

Detection itself is pure (``nab.series``); this controller wires it to the
live source — scanning a local directory on a worker thread, or reading the
open torrent's file list — and owns the prev/next buttons and episode picker.
Switching episodes is delegated back to the window via callbacks.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from nab import MEDIA_EXTENSIONS  # noqa: E402
from nab.resolver import ResolvedSource, SourceType  # noqa: E402
from nab.resolver.source import join_torrent_fragment, split_torrent_fragment  # noqa: E402
from nab.series import SeriesItem, SeriesView, detect_series  # noqa: E402

log = logging.getLogger(__name__)


def _is_media_filename(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_EXTENSIONS


def _torrent_engine():
    """Return the torrent engine, or None if libtorrent isn't installed."""
    from nab.torrent import is_available
    if not is_available():
        return None
    from nab.torrent.engine import get_engine
    return get_engine()


class SeriesNavigator:
    """Owns the series prev/next + picker widgets and their detection logic."""

    def __init__(
        self,
        *,
        play_input: Callable[[str], None],
        save_position: Callable[[], None],
    ) -> None:
        self._play_input = play_input
        self._save_position = save_position
        self._current_source: ResolvedSource | None = None
        self._current_series: SeriesView | None = None
        self._token = 0
        self._build_widgets()

    def _build_widgets(self) -> None:
        self.separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        self.prev_button = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        self.prev_button.set_tooltip_text("Previous in series")
        self.prev_button.connect("clicked", self._on_prev_clicked)

        self.popover = Gtk.Popover()
        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_has_frame(False)
        self.menu_button.set_tooltip_text("Show all in series")
        self.menu_button.set_popover(self.popover)

        self.next_button = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        self.next_button.set_tooltip_text("Next in series")
        self.next_button.connect("clicked", self._on_next_clicked)

        self._widgets = (
            self.separator,
            self.prev_button,
            self.menu_button,
            self.next_button,
        )
        for w in self._widgets:
            w.set_visible(False)

    @property
    def bar_widgets(self) -> tuple[Gtk.Widget, ...]:
        """Widgets to append to the transport bar, in order."""
        return self._widgets

    def update(self, source: ResolvedSource) -> None:
        """Refresh the series widget for the active source.

        Local files trigger filesystem-backed detection on a worker thread.
        Torrents resolve immediately from the (already-loaded) torrent file
        list. Other source types just hide the widget.
        """
        self._current_source = source
        self._token += 1
        token = self._token

        if source.source_type is SourceType.LOCAL_FILE:
            path = Path(source.playable_url)

            def worker():
                try:
                    siblings = [
                        p.name
                        for p in path.parent.iterdir()
                        if p.is_file()
                        and p.name != path.name
                        and _is_media_filename(p.name)
                    ]
                    view = detect_series(path.name, siblings)
                except OSError:
                    log.exception("series detection failed for %s", path)
                    view = None
                GLib.idle_add(self._apply, view, token)

            threading.Thread(target=worker, name="series-detect", daemon=True).start()
            return

        if source.source_type.is_torrent:
            view = self._torrent_series_view(source)
            self._apply(view, token)
            return

        self._apply(None, token)

    def _torrent_series_view(self, source: ResolvedSource) -> SeriesView | None:
        """Build the series view for the currently-open torrent.

        Falls back to a synthetic "all videos in torrent" view when the
        filenames don't form a series — every bundled file is intentional,
        so the user always gets a picker for a multi-file torrent.
        """
        engine = _torrent_engine()
        if engine is None:
            return None
        info = engine.current_info()
        if info is None or len(info.video_files) < 2:
            return None

        _, requested_sub_path = split_torrent_fragment(source.original_input)
        current = None
        if requested_sub_path is not None:
            current = info.by_sub_path(requested_sub_path)
        if current is None:
            current = info.by_index(engine.current_file_index())
        if current is None:
            return None
        current_name = current.name

        all_names = [f.name for f in info.video_files]
        siblings = [n for n in all_names if n != current_name]
        view = detect_series(current_name, siblings)
        if view is not None:
            return view

        # Synthetic fallback: position-numbered list of every video file.
        items = tuple(
            SeriesItem(name=n, number=i + 1) for i, n in enumerate(all_names)
        )
        current_index = next(
            i for i, n in enumerate(all_names) if n == current_name
        )
        return SeriesView(items=items, current_index=current_index)

    def _apply(self, view: SeriesView | None, token: int) -> bool:
        # Drop stale results from a previous file.
        if token != self._token:
            return False
        self._current_series = view
        show = view is not None
        for w in self._widgets:
            w.set_visible(show)
        if view is None:
            return False
        self.prev_button.set_sensitive(view.prev is not None)
        self.next_button.set_sensitive(view.next is not None)
        self.menu_button.set_label(f"{view.current_index + 1} of {len(view.items)}")
        self._build_popover(view)
        return False

    def _build_popover(self, view: SeriesView) -> None:
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        for i, item in enumerate(view.items):
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=item.name, xalign=0.0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            if i == view.current_index:
                label.add_css_class("heading")
            row.set_child(label)
            row.item_name = item.name
            listbox.append(row)
        listbox.connect("row-activated", self._on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_width(320)
        scrolled.set_max_content_height(420)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_child(listbox)
        self.popover.set_child(scrolled)

    def _on_prev_clicked(self, _b: Gtk.Button) -> None:
        if self._current_series is None:
            return
        item = self._current_series.prev
        if item is not None:
            self._switch_to_sibling(item.name)

    def _on_next_clicked(self, _b: Gtk.Button) -> None:
        if self._current_series is None:
            return
        item = self._current_series.next
        if item is not None:
            self._switch_to_sibling(item.name)

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        name = getattr(row, "item_name", None)
        if name is None:
            return
        self.popover.popdown()
        self._switch_to_sibling(name)

    def _switch_to_sibling(self, sibling_name: str) -> None:
        """Play the sibling identified by ``sibling_name`` inside the current source.

        For local files we just play the path next door. For torrents we
        translate the filename back to its sub-path in the torrent and
        replay through the resolver — the engine's info-hash cache turns
        the second open_torrent into a no-op so there's no metadata wait.
        """
        source = self._current_source
        if source is None:
            return

        if source.source_type is SourceType.LOCAL_FILE:
            parent = Path(source.playable_url).parent
            self._play_input(str(parent / sibling_name))
            return

        if source.source_type.is_torrent:
            engine = _torrent_engine()
            if engine is None:
                return
            info = engine.current_info()
            if info is None:
                return
            target = info.by_name(sibling_name)
            if target is None:
                log.warning("series row %r not found in torrent", sibling_name)
                return
            # Persist the outgoing episode's position before we swap files.
            self._save_position()
            base_uri, _ = split_torrent_fragment(source.original_input)
            self._play_input(join_torrent_fragment(base_uri, target.sub_path))
