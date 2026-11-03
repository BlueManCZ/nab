"""The library: a shelf of shows and movies, and a page per show.

The root page leads with "Continue Watching" — one click resumes the right
episode — over a poster grid of every title. A show's page lists its
episodes by season with their watched / in-progress / up-next state. Playing
is delegated back to the window; marking and removing go straight to the
history and library stores, followed by a refresh.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from nab.history import HistoryDatabase  # noqa: E402
from nab.library import style  # noqa: E402
from nab.library.catalog import Episode, Kind, Title, build_catalog  # noqa: E402
from nab.library.store import LibraryStore  # noqa: E402
from nab.resolver.source import (  # noqa: E402
    TORRENT_SOURCE_TYPES,
    SourceType,
    classify,
    split_torrent_fragment,
)

log = logging.getLogger(__name__)

_ROOT_TAG = "library"
_CONTINUE_LIMIT = 6
_CARD_SIZE = (150, 216)
_HERO_SIZE = _CARD_SIZE
_ROW_POSTER_SIZE = (40, 56)


def _format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{minutes} min"


def _format_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    return f"{n / (1 << 20):.0f} MB"


def _remaining_text(episode: Episode) -> str | None:
    remaining = episode.remaining
    return f"{_format_duration(remaining)} left" if remaining else None


def _card_caption(title: Title) -> str:
    """One line under a poster: where the user is with this title."""
    next_up = title.next_up
    if title.kind is Kind.MOVIE:
        movie = title.episodes[0]
        if movie.watched:
            return "Watched"
        return _remaining_text(movie) if movie.in_progress else "Movie"
    if next_up is None:
        return "Watched"
    if next_up.in_progress:
        return f"{next_up.label} · {_remaining_text(next_up)}"
    if title.started:
        return f"Up next · {next_up.label}"
    count = len(title.episodes)
    return f"{count} episode" if count == 1 else f"{count} episodes"


def _episode_subtitle(episode: Episode, *, playing: bool) -> str:
    if playing:
        return "Now playing"
    if episode.in_progress:
        return _remaining_text(episode) or "In progress"
    if episode.watched:
        return "Watched"
    p = episode.progress
    if p is not None and p.duration:
        return _format_duration(p.duration)
    return _format_size(episode.size)


def _poster(title: Title, size: tuple[int, int], *, variant: str) -> Gtk.Widget:
    """A generated poster: the title set on a gradient, with progress.

    `variant` is "small" (just the initial, for list rows) or "card".
    """
    width, height = size
    overlay = Gtk.Overlay()
    overlay.set_size_request(width, height)
    overlay.set_halign(Gtk.Align.START)
    overlay.set_valign(Gtk.Align.START)
    overlay.set_overflow(Gtk.Overflow.HIDDEN)
    for css in ("nab-poster", style.poster_class(title.key), variant):
        overlay.add_css_class(css)

    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    inner.add_css_class("nab-poster-inner")
    if variant == "small":
        initial = Gtk.Label(label=title.name[:1].upper())
        initial.add_css_class("nab-poster-title")
        initial.set_vexpand(True)
        inner.append(initial)
    else:
        name = Gtk.Label(label=title.name, xalign=0.0, yalign=0.0)
        name.set_wrap(True)
        name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        name.set_lines(5)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(1)
        name.set_vexpand(True)
        name.set_valign(Gtk.Align.START)
        name.add_css_class("nab-poster-title")
        inner.append(name)

        meta = title.season_span if title.kind is Kind.SHOW else ""
        if title.year and not meta:
            meta = str(title.year)
        if meta:
            meta_label = Gtk.Label(label=meta, xalign=0.0)
            meta_label.set_max_width_chars(1)
            meta_label.set_ellipsize(Pango.EllipsizeMode.END)
            meta_label.add_css_class("nab-poster-meta")
            inner.append(meta_label)

        fraction = title.fraction
        if fraction >= 1.0:
            badge = Gtk.Image.new_from_icon_name("object-select-symbolic")
            badge.set_halign(Gtk.Align.END)
            badge.set_valign(Gtk.Align.END)
            badge.add_css_class("nab-poster-badge")
            badge.set_tooltip_text("Watched")
            overlay.add_overlay(badge)
        elif fraction > 0.0:
            bar = Gtk.ProgressBar(fraction=max(fraction, 0.04))
            bar.add_css_class("nab-progress")
            bar.set_margin_top(6)
            inner.append(bar)
    overlay.set_child(inner)
    return overlay


def _menu_item(label: str, action: str, target: str) -> Gio.MenuItem:
    item = Gio.MenuItem.new(label, None)
    item.set_action_and_target_value(f"library.{action}", GLib.Variant.new_string(target))
    return item


def _title_menu(title: Title) -> Gio.Menu:
    menu = Gio.Menu()
    marks = Gio.Menu()
    if title.fraction < 1.0:
        marks.append_item(_menu_item("Mark as Watched", "title-watched", title.key))
    if title.started:
        marks.append_item(_menu_item("Mark as Unwatched", "title-unwatched", title.key))
    menu.append_section(None, marks)
    remove = Gio.Menu()
    remove.append_item(_menu_item("Remove from Library", "remove", title.key))
    menu.append_section(None, remove)
    return menu


class LibraryView:
    """Owns the library's navigation view: the shelf and per-title pages."""

    def __init__(
        self,
        history: HistoryDatabase,
        store: LibraryStore,
        *,
        header: Gtk.Widget,
        play: Callable[[str], None],
        add_toast: Callable[[Adw.Toast], None],
        cache_dir: Callable[[], Path | None],
    ) -> None:
        style.install()
        self._history = history
        self._store = store
        self._play = play
        self._add_toast = add_toast
        self._cache_dir = cache_dir
        self._catalog: list[Title] = []
        self._titles: dict[str, Title] = {}
        self._by_input: dict[str, tuple[Title, Episode]] = {}
        self._current_input: str | None = None
        # The open title page, if any: (page, its scrolled window, title key).
        self._title_page: tuple[Adw.NavigationPage, Gtk.ScrolledWindow, str] | None = None
        # The row to focus when the title page appears (the up-next episode).
        self._focus_row: Gtk.Widget | None = None

        self._nav = Adw.NavigationView()
        self._nav.connect("popped", self._on_popped)
        self._nav.insert_action_group("library", self._build_actions())
        self._nav.add(self._build_root(header))

    @property
    def widget(self) -> Gtk.Widget:
        return self._nav

    # ------------------------------------------------------------- public API

    def set_current(self, input: str | None) -> None:
        """Mark what's playing, so its row and card say so. Refreshes."""
        self._current_input = input
        self.refresh()

    def refresh(self) -> None:
        """Rebuild from the stores, keeping the open page and scroll position."""
        cache_dir = self._cache_dir()
        progress = self._store.progress_index(
            self._history.entries_of(t.value for t in TORRENT_SOURCE_TYPES),
            self._history.entries_of([SourceType.LOCAL_FILE.value]),
            cache_dir,
        )
        self._catalog = build_catalog(self._store.torrents(), progress)
        self._titles = {t.key: t for t in self._catalog}
        self._by_input = {
            input: (title, episode)
            for title in self._catalog
            for episode in title.episodes + title.extras
            for input in episode.inputs
        }
        self._fill_root()

        if self._title_page is not None:
            page, scrolled, key = self._title_page
            title = self._titles.get(key)
            if title is None:
                self._nav.pop_to_tag(_ROOT_TAG)
            else:
                page.set_title(title.name)
                page.menu_button.set_menu_model(_title_menu(title))
                _replace_preserving_scroll(scrolled, self._build_title_content(title))

    def describe(self, input: str) -> str | None:
        """A human title for `input` if it's in the library — `Show · S01E02 · Name`."""
        found = self._by_input.get(input)
        if found is None:
            return None
        title, episode = found
        if title.kind is Kind.MOVIE and episode in title.episodes:
            return title.name
        parts = [title.name, episode.label, episode.display_name]
        return " · ".join(p for p in parts if p)

    def show_title_for(self, input: str) -> bool:
        """Open the page of the title `input` belongs to. False if it has none."""
        found = self._by_input.get(input)
        if found is None:
            return False
        title, _ = found
        if title.kind is Kind.MOVIE:
            return False
        self._open_title(title)
        return True

    def backfill_async(self) -> None:
        """Shelve torrents from history that predate the library.

        Reads each one's metadata from resume data or its .torrent file on a
        worker thread — never the swarm — and records what it finds. Runs
        once per start; torrents already known (removed ones included) are
        skipped.
        """
        from nab.torrent import is_available
        if not is_available():
            return
        known = self._store.known_sources()
        sources = [
            base
            for base in dict.fromkeys(
                split_torrent_fragment(i)[0] for i in self._history.torrent_inputs()
            )
            if base not in known
        ]
        if not sources:
            return

        def worker():
            from nab.torrent.engine import read_cached_info
            found = []
            for source in sources:
                try:
                    info = read_cached_info(source)
                except Exception:
                    log.exception("couldn't read cached metadata for %s", source)
                    continue
                if info is not None:
                    found.append((source, info))
            GLib.idle_add(self._apply_backfill, found)

        threading.Thread(target=worker, name="library-backfill", daemon=True).start()

    def _apply_backfill(self, found) -> bool:
        for source, info in found:
            self._store.add(source, info, reveal=False)
        log.info("library backfill shelved %d torrent(s)", len(found))
        if found:
            self.refresh()
        return False

    # --------------------------------------------------------------- root page

    def _build_root(self, header: Gtk.Widget) -> Adw.NavigationPage:
        empty = Adw.StatusPage()
        empty.set_icon_name("video-x-generic-symbolic")
        empty.set_title("Nab anything")
        empty.set_description(
            "Paste a URL, magnet, or file path above and press Enter — "
            "a .torrent file works too. Torrents you nab collect here, "
            "ready to pick up where you left off."
        )

        self._root_scrolled = Gtk.ScrolledWindow()
        self._root_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._root_stack = Gtk.Stack()
        self._root_stack.add_named(empty, "empty")
        self._root_stack.add_named(self._root_scrolled, "shelf")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._root_stack)
        return Adw.NavigationPage(child=toolbar, title="Library", tag=_ROOT_TAG)

    def _fill_root(self) -> None:
        if not self._catalog:
            self._root_stack.set_visible_child_name("empty")
            return
        self._root_stack.set_visible_child_name("shelf")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=30)
        box.set_margin_top(24)
        box.set_margin_bottom(30)
        box.set_margin_start(18)
        box.set_margin_end(18)

        resumable = [
            t for t in self._catalog
            if (n := t.next_up) is not None
            and (n.in_progress or (t.kind is Kind.SHOW and t.started))
        ][:_CONTINUE_LIMIT]
        if resumable:
            group = Adw.PreferencesGroup(title="Continue Watching")
            for title in resumable:
                group.add(self._continue_row(title))
            box.append(group)

        shelf = Gtk.FlowBox()
        shelf.set_selection_mode(Gtk.SelectionMode.NONE)
        shelf.set_homogeneous(True)
        shelf.set_valign(Gtk.Align.START)
        shelf.set_min_children_per_line(2)
        shelf.set_max_children_per_line(12)
        shelf.set_column_spacing(6)
        shelf.set_row_spacing(12)
        shelf.connect("child-activated", self._on_card_activated)
        for title in self._catalog:
            shelf.append(self._card(title))

        group = Adw.PreferencesGroup(title="Library")
        group.add(shelf)
        box.append(group)

        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900)
        clamp.set_child(box)
        _replace_preserving_scroll(self._root_scrolled, clamp)

    def _continue_row(self, title: Title) -> Gtk.Widget:
        episode = title.next_up
        assert episode is not None
        row = Adw.ActionRow(title=title.name, use_markup=False, activatable=True)
        if title.kind is Kind.MOVIE:
            subtitle = _remaining_text(episode) or ""
        else:
            parts = [episode.label, episode.name]
            if episode.in_progress:
                parts.append(_remaining_text(episode))
            subtitle = " · ".join(p for p in parts if p)
        row.set_subtitle(subtitle)
        row.add_prefix(_poster(title, _ROW_POSTER_SIZE, variant="small"))

        if episode.in_progress:
            bar = Gtk.ProgressBar(fraction=episode.fraction)
            bar.add_css_class("nab-progress")
            bar.set_valign(Gtk.Align.CENTER)
            bar.set_size_request(72, -1)
            row.add_suffix(bar)
        icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        row.add_suffix(icon)
        row.set_tooltip_text(f"{'Resume' if episode.in_progress else 'Play'} {episode.label}".strip())
        row.connect("activated", lambda _r: self._play(episode.input))
        return row

    def _card(self, title: Title) -> Gtk.FlowBoxChild:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(_poster(title, _CARD_SIZE, variant="card"))
        caption = Gtk.Label(label=_card_caption(title), xalign=0.0)
        caption.set_max_width_chars(1)
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        caption.set_margin_start(2)
        box.append(caption)

        child = Gtk.FlowBoxChild(child=box)
        child.add_css_class("nab-card")
        child.set_tooltip_text(title.name)
        child.title_key = title.key

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._on_card_context, child, title)
        child.add_controller(click)
        return child

    def _on_card_context(self, _gesture, _n, x: float, y: float, child, title: Title) -> None:
        popover = Gtk.PopoverMenu.new_from_model(_title_menu(title))
        popover.set_parent(child)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _on_card_activated(self, _box: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        title = self._titles.get(getattr(child, "title_key", ""))
        if title is None:
            return
        if title.kind is Kind.MOVIE:
            # A movie is one thing to watch; its card plays (or resumes) it.
            self._play(title.episodes[0].input)
        else:
            self._open_title(title)

    # -------------------------------------------------------------- title page

    def _open_title(self, title: Title) -> None:
        open_page = self._title_page
        if open_page is not None and open_page[2] == title.key and (
            self._nav.get_visible_page() is open_page[0]
        ):
            return
        self._nav.pop_to_tag(_ROOT_TAG)

        menu_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu_button.set_tooltip_text("More")
        menu_button.set_menu_model(_title_menu(title))
        header = Adw.HeaderBar()
        header.pack_end(menu_button)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self._build_title_content(title))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scrolled)
        page = Adw.NavigationPage(child=toolbar, title=title.name, tag=f"title:{title.key}")
        page.menu_button = menu_button
        page.connect("shown", self._on_title_shown)
        self._title_page = (page, scrolled, title.key)
        self._nav.push(page)

    def _on_title_shown(self, _page: Adw.NavigationPage) -> None:
        # Land on the episode the user is most likely after.
        if self._focus_row is not None:
            self._focus_row.grab_focus()

    def _on_popped(self, _nav, page: Adw.NavigationPage) -> None:
        if self._title_page is not None and self._title_page[0] is page:
            self._title_page = None

    def _build_title_content(self, title: Title) -> Gtk.Widget:
        self._focus_row = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=30)
        box.set_margin_top(24)
        box.set_margin_bottom(30)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(self._build_hero(title))

        next_up = title.next_up
        for season in title.seasons:
            heading = (
                f"Season {season.number}" if season.number is not None else "Episodes"
            )
            group = Adw.PreferencesGroup(title=heading)
            count = Gtk.Label(
                label=f"{season.watched_count} of {len(season.episodes)} watched"
            )
            count.add_css_class("caption")
            count.add_css_class("dim-label")
            group.set_header_suffix(count)
            for episode in season.episodes:
                row = self._episode_row(episode, up_next=episode is next_up)
                if episode is next_up:
                    self._focus_row = row
                group.add(row)
            box.append(group)

        if title.extras:
            group = Adw.PreferencesGroup(title="Extras")
            expander = Adw.ExpanderRow(
                title=f"{len(title.extras)} video{'s' if len(title.extras) != 1 else ''}",
                subtitle="Samples, featurettes and other bonus material",
            )
            for extra in title.extras:
                expander.add_row(self._episode_row(extra, up_next=False))
            group.add(expander)
            box.append(group)

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=600)
        clamp.set_child(box)
        return clamp

    def _build_hero(self, title: Title) -> Gtk.Widget:
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info.set_valign(Gtk.Align.CENTER)
        info.set_hexpand(True)

        name = Gtk.Label(label=title.name, xalign=0.0)
        name.set_wrap(True)
        name.add_css_class("title-1")
        info.append(name)

        episodes = title.episodes
        meta = [str(title.year) if title.year else "", title.season_span]
        if title.kind is Kind.SHOW:
            meta.append(f"{len(episodes)} episode{'s' if len(episodes) != 1 else ''}")
        meta.append(_format_size(title.total_size))
        meta_label = Gtk.Label(label=" · ".join(m for m in meta if m), xalign=0.0)
        meta_label.set_wrap(True)
        meta_label.add_css_class("dim-label")
        info.append(meta_label)

        if title.kind is Kind.SHOW:
            progress = Gtk.Label(
                label=f"{title.watched_count} of {len(episodes)} watched", xalign=0.0
            )
            progress.add_css_class("caption")
            progress.add_css_class("dim-label")
            info.append(progress)

        verb, episode = self._primary_action(title)
        label = f"{verb} {episode.label}" if title.kind is Kind.SHOW else verb
        button = Gtk.Button(child=Adw.ButtonContent(
            icon_name="media-playback-start-symbolic", label=label
        ))
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.set_halign(Gtk.Align.START)
        button.set_margin_top(12)
        button.connect("clicked", lambda _b: self._play(episode.input))
        info.append(button)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        hero.append(_poster(title, _HERO_SIZE, variant="card"))
        hero.append(info)
        return hero

    @staticmethod
    def _primary_action(title: Title) -> tuple[str, Episode]:
        next_up = title.next_up
        if next_up is None:
            return "Watch Again", title.episodes[0]
        if next_up.in_progress:
            return "Resume", next_up
        if title.started:
            return "Continue", next_up
        return "Play", next_up

    def _episode_row(self, episode: Episode, *, up_next: bool) -> Adw.ActionRow:
        playing = self._current_input is not None and self._current_input in episode.inputs
        row = Adw.ActionRow(title=episode.display_name, use_markup=False, activatable=True)
        row.set_subtitle(_episode_subtitle(episode, playing=playing))
        row.set_tooltip_text(episode.file_name)

        if episode.number is not None:
            number = Gtk.Label(label=str(episode.number), xalign=1.0)
            number.add_css_class("nab-episode-number")
            number.add_css_class("numeric")
            number.add_css_class("dim-label")
            row.add_prefix(number)

        if playing:
            icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            icon.add_css_class("accent")
            row.add_suffix(icon)
        elif episode.in_progress:
            bar = Gtk.ProgressBar(fraction=episode.fraction)
            bar.add_css_class("nab-progress")
            bar.set_valign(Gtk.Align.CENTER)
            bar.set_size_request(72, -1)
            row.add_suffix(bar)
        elif episode.watched:
            icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
            icon.add_css_class("success")
            row.add_suffix(icon)
            row.add_css_class("nab-watched")
        if up_next and not playing:
            badge = Gtk.Label(label="UP NEXT")
            badge.set_valign(Gtk.Align.CENTER)
            badge.add_css_class("nab-up-next")
            row.add_suffix(badge)

        menu = Gio.Menu()
        if episode.watched or episode.in_progress:
            menu.append_item(_menu_item("Mark as Unwatched", "mark-unwatched", episode.input))
        if not episode.watched:
            menu.append_item(_menu_item("Mark as Watched", "mark-watched", episode.input))
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu)
        more.set_valign(Gtk.Align.CENTER)
        more.add_css_class("flat")
        more.set_tooltip_text("More")
        row.add_suffix(more)

        row.connect("activated", lambda _r: self._play(episode.input))
        return row

    # ---------------------------------------------------------------- actions

    def _build_actions(self) -> Gio.SimpleActionGroup:
        group = Gio.SimpleActionGroup()
        handlers = {
            "mark-watched": lambda input: self._mark_episodes([input], True),
            "mark-unwatched": lambda input: self._mark_episodes([input], False),
            "title-watched": lambda key: self._mark_title(key, True),
            "title-unwatched": lambda key: self._mark_title(key, False),
            "remove": self._remove_title,
        }
        for name, handler in handlers.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect(
                "activate", lambda _a, param, h=handler: h(param.get_string())
            )
            group.add_action(action)
        return group

    def _set_watched(self, episode: Episode, watched: bool) -> None:
        # Unwatching has to clear every copy: any finished one counts.
        for input in episode.inputs if not watched else (episode.input,):
            self._history.set_watched(
                input,
                source_type=classify(input).value,
                title=episode.file_name,
                watched=watched,
            )

    def _mark_episodes(self, inputs: list[str], watched: bool) -> None:
        for input in inputs:
            found = self._by_input.get(input)
            if found is not None:
                self._set_watched(found[1], watched)
        self.refresh()

    def _mark_title(self, key: str, watched: bool) -> None:
        title = self._titles.get(key)
        if title is None:
            return
        for episode in title.episodes:
            if episode.watched != watched or (not watched and episode.in_progress):
                self._set_watched(episode, watched)
        self.refresh()

    def _remove_title(self, key: str) -> None:
        title = self._titles.get(key)
        if title is None:
            return
        hashes = list(title.info_hashes)
        self._store.set_hidden(hashes, True)
        self.refresh()

        toast = Adw.Toast.new(f"Removed “{title.name}” from the library")
        toast.set_button_label("Undo")
        toast.connect("button-clicked", lambda _t: self._undo_remove(hashes))
        self._add_toast(toast)

    def _undo_remove(self, hashes: list[str]) -> None:
        self._store.set_hidden(hashes, False)
        self.refresh()


def _replace_preserving_scroll(scrolled: Gtk.ScrolledWindow, child: Gtk.Widget) -> None:
    """Swap `scrolled`'s content without jumping back to the top."""
    adjustment = scrolled.get_vadjustment()
    value = adjustment.get_value()
    scrolled.set_child(child)
    if value <= 0:
        return

    def restore() -> bool:
        adjustment.set_value(value)
        return False

    # The new child hasn't been measured yet, so the adjustment's range is
    # stale; restore once layout has caught up.
    GLib.idle_add(restore)
