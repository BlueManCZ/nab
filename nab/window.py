import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from nab import MEDIA_EXTENSIONS  # noqa: E402
from nab.gtkutil import log_dialog_error  # noqa: E402
from nab.history import HistoryDatabase  # noqa: E402
from nab.history.sidebar import HistorySidebar  # noqa: E402
from nab.playback import PlaybackConfig  # noqa: E402
from nab.player import Player  # noqa: E402
from nab.resolver import ResolvedSource, resolve  # noqa: E402
from nab.resolver.source import TORRENT_FILE_SUFFIX, torrent_for_cache_path  # noqa: E402
from nab.series.nav import SeriesNavigator  # noqa: E402
from nab.subtitles import SubtitlesConfig  # noqa: E402
from nab.torrent.stats_view import TorrentStatsView  # noqa: E402
from nab.subtitles.panel import SubtitlePanel  # noqa: E402

log = logging.getLogger(__name__)

# Don't offer to resume a file the user barely started.
_RESUME_MIN_SECONDS = 5.0
# How often to persist the current playback position to history.
_POSITION_SAVE_INTERVAL = 5
# How often to refresh the live torrent stats widget on the now-playing card.
_TORRENT_STATS_INTERVAL = 1
# Debounce window for persisting the volume; the slider fires continuously
# while dragged, so we coalesce writes to config.toml.
_VOLUME_SAVE_DELAY_MS = 500


def _format_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _engine_status():
    """Live torrent stats from the shared engine, or None when unavailable."""
    from nab.torrent import is_available
    if not is_available():
        return None
    from nab.torrent.engine import engine_status
    return engine_status()


class _UpdateGuard:
    """Reentrancy flag for code-driven `Gtk.Adjustment` changes.

    A `Gtk.Adjustment` fires `value-changed` whether we push mpv's value in or
    the user drags the widget. Wrap our own writes in `with guard:` so the
    `value-changed` handler can skip them (`if guard: return`) and only forward
    genuine user input back to mpv.
    """

    def __init__(self) -> None:
        self._active = False

    def __bool__(self) -> bool:
        return self._active

    def __enter__(self) -> None:
        self._active = True

    def __exit__(self, *_exc) -> bool:
        self._active = False
        return False


class NabWindow(Adw.ApplicationWindow):
    __gtype_name__ = "NabWindow"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(960, 600)
        self.set_title("Nab")

        self.history = HistoryDatabase()
        self.history_sidebar = HistorySidebar(
            self.history, on_activate=self._on_history_activate
        )
        self.player: Player | None = None
        self._current_entry_id: int | None = None
        self._current_source: ResolvedSource | None = None
        self._duration: float | None = None
        self._position: float = 0.0
        self._is_paused: bool = True
        self._seek_guard = _UpdateGuard()
        self._volume_guard = _UpdateGuard()
        self._playback_config = PlaybackConfig.load()
        self._save_position_source: int | None = None
        self._torrent_stats_source: int | None = None
        self._volume_save_source: int | None = None
        # mpv is started on demand (and re-started after the user closes its
        # window). _pending_source holds what to play once a freshly-spawned
        # mpv is ready; _mpv_starting guards against spawning two at once.
        self._pending_source: ResolvedSource | None = None
        self._mpv_starting: bool = False

        self.series_nav = SeriesNavigator(
            play_input=self.play_input,
            save_position=lambda: self._save_position_now(False),
        )
        self.subtitle_panel = SubtitlePanel(
            SubtitlesConfig.load(),
            get_player=lambda: self.player,
            show_toast=self._show_toast,
        )
        self.torrent_stats = TorrentStatsView()

        self._build_ui()
        # No mpv yet — its window only opens once there's something to play.

        self.connect("close-request", self._on_close_request)
        self.history_sidebar.refresh()

    # ---------------------------------------------------------------- UI build

    def _build_ui(self) -> None:
        # Sidebar = history. Content = main area.
        self.split = Adw.OverlaySplitView()
        self.split.set_show_sidebar(False)
        self.split.set_min_sidebar_width(280)
        self.split.set_max_sidebar_width(360)
        self.split.set_sidebar(self.history_sidebar.widget)
        # Toasts overlay the content area (not the sidebar), so the overlay
        # wraps the content rather than the whole split.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._build_content())
        self.split.set_content(self._toast_overlay)
        self.set_content(self.split)

        # Action: toggle sidebar
        self.toggle_sidebar_button.connect(
            "clicked", lambda _b: self.split.set_show_sidebar(not self.split.get_show_sidebar())
        )

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        toolbar.set_content(self._build_video_area())
        toolbar.add_bottom_bar(self._build_controls())
        return toolbar

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self.toggle_sidebar_button = Gtk.ToggleButton()
        self.toggle_sidebar_button.set_icon_name("sidebar-show-symbolic")
        self.toggle_sidebar_button.set_tooltip_text("Toggle history")
        header.pack_start(self.toggle_sidebar_button)

        self.open_file_button = Gtk.Button()
        self.open_file_button.set_icon_name("document-open-symbolic")
        self.open_file_button.set_tooltip_text("Open local file…")
        self.open_file_button.connect("clicked", self._on_open_file_clicked)
        header.pack_start(self.open_file_button)

        # Single input row in the title area
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_box.set_hexpand(True)
        title_box.set_halign(Gtk.Align.FILL)

        self.input_entry = Gtk.Entry()
        self.input_entry.set_placeholder_text(
            "Paste a URL, magnet, .torrent, or file path…"
        )
        self.input_entry.set_hexpand(True)
        self.input_entry.connect("activate", self._on_input_activate)
        title_box.append(self.input_entry)

        self.play_button = Gtk.Button()
        self.play_button.set_icon_name("media-playback-start-symbolic")
        self.play_button.add_css_class("suggested-action")
        self.play_button.set_tooltip_text("Nab")
        self.play_button.connect("clicked", lambda _b: self._on_input_activate(self.input_entry))
        title_box.append(self.play_button)

        self.input_spinner = Gtk.Spinner()
        self.input_spinner.set_visible(False)
        title_box.append(self.input_spinner)

        header.set_title_widget(title_box)

        # Hamburger menu
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("About Nab", "app.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        return header

    def _build_video_area(self) -> Gtk.Widget:
        # mpv runs in its own window — Wayland has no protocol for embedding
        # a foreign-process surface today, and libmpv's render API would cost
        # us gpu-next/HDR. So Nab's window is the controller and mpv is the
        # player. This placeholder describes what's loaded.
        self.video_status_page = Adw.StatusPage()
        self.video_status_page.set_icon_name("video-x-generic-symbolic")
        self.video_status_page.set_title("Nab anything")
        self.video_status_page.set_description(
            "Paste a URL, magnet, or file path above and press Enter — "
            "a .torrent file works too. The video plays in mpv's own window."
        )
        self.video_status_page.set_vexpand(True)
        # Live torrent dashboard lives in the status page's content slot; it
        # only becomes visible while a torrent is streaming (see _loadfile_now).
        self.torrent_stats.widget.set_visible(False)
        self.video_status_page.set_child(self.torrent_stats.widget)
        return self.video_status_page

    def _build_controls(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)

        self.play_pause_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_pause_button.connect("clicked", self._on_play_pause_clicked)
        self.play_pause_button.set_sensitive(False)
        bar.append(self.play_pause_button)

        self.position_label = Gtk.Label(label="--:--")
        self.position_label.add_css_class("numeric")
        bar.append(self.position_label)

        self.seek_adjustment = Gtk.Adjustment(lower=0, upper=1, value=0)
        self.seek_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.seek_adjustment)
        self.seek_scale.set_draw_value(False)
        self.seek_scale.set_hexpand(True)
        self.seek_scale.set_sensitive(False)
        # This fires whether we push mpv's position in or the user drags the
        # slider; `self._seek_guard` lets the handler tell the two apart (see
        # _UpdateGuard) so only genuine user seeks are forwarded back to mpv.
        self.seek_adjustment.connect("value-changed", self._on_seek_value_changed)
        bar.append(self.seek_scale)

        self.duration_label = Gtk.Label(label="--:--")
        self.duration_label.add_css_class("numeric")
        bar.append(self.duration_label)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        bar.append(sep)

        vol_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        bar.append(vol_icon)
        self.volume_adjustment = Gtk.Adjustment(
            lower=0,
            upper=100,
            value=self._playback_config.effective_volume,
            step_increment=5,
        )
        self.volume_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.volume_adjustment)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(120, -1)
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        bar.append(self.volume_scale)

        # Subtitle re-fetch button + spinner. Hidden until relevant.
        for w in self.subtitle_panel.bar_widgets:
            bar.append(w)

        # Series prev/next + picker. Hidden until a series is detected.
        for w in self.series_nav.bar_widgets:
            bar.append(w)

        return bar

    # ----------------------------------------------------------- mpv lifecycle

    def _start_mpv_async(self) -> None:
        def worker():
            try:
                player = Player()
                # Subscribe to the properties we drive UI off of. mpv fires
                # these on its IPC thread, so each observer marshals onto the
                # GTK main loop (see _observe_main). pause maps None->False;
                # the rest skip None updates.
                self._observe_main(player, "time-pos", self._apply_time_pos, float)
                self._observe_main(player, "duration", self._apply_duration, float)
                self._observe_main(player, "pause", self._apply_pause, bool, skip_none=False)
                self._observe_main(player, "media-title", self._apply_media_title, str)
                self._observe_main(player, "volume", self._apply_volume, float)
                player.on_event("end-file", self._on_end_file)
                # The user may close mpv's window at any time; the socket dies
                # and we treat it as Stop. Bind to *this* player so a late
                # signal from an old session can't tear down a newer one.
                player.on_disconnect(lambda: GLib.idle_add(self._on_mpv_disconnected, player))
                # Initial volume
                player.set_property("volume", self.volume_adjustment.get_value())
                self.player = player
                GLib.idle_add(self._on_mpv_ready)
            except Exception as exc:
                log.exception("failed to start mpv")
                GLib.idle_add(self._on_mpv_failed, str(exc))

        threading.Thread(target=worker, name="mpv-startup", daemon=True).start()

    def _on_mpv_ready(self) -> bool:
        self._mpv_starting = False
        log.info("mpv ready")
        if self._pending_source is not None:
            source = self._pending_source
            self._pending_source = None
            self._loadfile_now(source)
        return False

    def queue_initial_play(self, text: str) -> None:
        """Play an input handed in at launch (file manager, CLI arg, magnet…).

        mpv is started on demand by the resolve→playback path, so we can just
        play; there's no player to wait for.
        """
        self.play_input(text)

    def _on_mpv_failed(self, msg: str) -> bool:
        self._mpv_starting = False
        self._pending_source = None
        self.video_status_page.set_title("Failed to start mpv")
        self.video_status_page.set_description(msg)
        self._show_toast(f"Couldn't start mpv: {msg}", timeout=6)
        return False

    def _on_mpv_disconnected(self, player: Player) -> bool:
        """The mpv window was closed (or mpv crashed): treat it as Stop.

        Persist where we were, drop the dead player, and reset transport.
        The next play spawns a fresh mpv window via `_begin_playback`.
        """
        if player is not self.player:
            # A newer mpv session already replaced this one — stale signal.
            return False
        log.info("mpv window closed — stopping playback")
        self._save_position_now(False)
        self._teardown_torrent(full=False)
        self._teardown_mpv()
        self._enter_stopped_state()
        return False

    def _teardown_mpv(self) -> None:
        """Drop the current player. Safe when mpv is already gone."""
        if self.player is not None:
            try:
                self.player.close()
            except Exception:
                pass
            self.player = None

    def _enter_stopped_state(self) -> None:
        """Reset transport UI to 'nothing playing' after a Stop."""
        if self._save_position_source is not None:
            GLib.source_remove(self._save_position_source)
            self._save_position_source = None
        self._stop_torrent_stats()
        self._current_entry_id = None
        self._is_paused = True
        self._position = 0.0
        self._duration = None
        self._update_play_pause_icon()
        self.play_pause_button.set_sensitive(False)
        self.seek_scale.set_sensitive(False)
        with self._seek_guard:
            self.seek_adjustment.set_value(0)
        self.position_label.set_text("--:--")
        self.duration_label.set_text("--:--")
        self.video_status_page.set_title("Playback stopped")
        self.video_status_page.set_description(
            "You closed the mpv window. Pick something from history or paste a "
            "new link to play again."
        )

    def shutdown_player(self) -> None:
        if self._save_position_source is not None:
            GLib.source_remove(self._save_position_source)
            self._save_position_source = None
        if self._volume_save_source is not None:
            GLib.source_remove(self._volume_save_source)
            self._volume_save_source = None
            self._save_volume_now(self.volume_adjustment.get_value())
        self._stop_torrent_stats()
        # Ask mpv to quit, then drop it. _teardown_mpv nulls self.player, so
        # the disconnect this provokes is recognised as stale and ignored.
        if self.player is not None:
            try:
                self.player.quit()
            except Exception:
                pass
        self._teardown_mpv()
        self._teardown_torrent(full=True)

    def _teardown_torrent(self, *, full: bool) -> None:
        """Release torrent resources, if libtorrent is even in use.

        `full=True` tears the whole engine down (app exit); otherwise it just
        stops serving the current torrent. No-op when libtorrent isn't
        installed — which is also why the engine import is guarded.
        """
        from nab.torrent import is_available
        if not is_available():
            return
        from nab.torrent.engine import cleanup_engine, shutdown_engine
        (shutdown_engine if full else cleanup_engine)()

    def _on_close_request(self, _w) -> bool:
        self.shutdown_player()
        return False

    # ------------------------------------------------------------- input flow

    def play_input(self, text: str) -> None:
        self.input_entry.set_text(text)
        self._on_input_activate(self.input_entry)

    def _on_open_file_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Open video or torrent")

        video_filter = Gtk.FileFilter()
        video_filter.set_name("Video, audio & torrents")
        for ext in (*MEDIA_EXTENSIONS, TORRENT_FILE_SUFFIX):
            bare = ext.lstrip(".")
            video_filter.add_pattern(f"*.{bare}")
            video_filter.add_pattern(f"*.{bare.upper()}")
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(video_filter)
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(video_filter)

        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error as exc:
            log_dialog_error(exc, "file dialog")
            return
        path = gfile.get_path() if gfile is not None else None
        if path:
            self.play_input(path)

    def _on_input_activate(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        log.info("input activate: %r", text)
        if not text:
            return
        self._resolve_async(text)

    def _resolve_async(self, text: str, *, recovering: bool = False) -> None:
        # mpv is spawned on demand once we know what to play (see
        # _begin_playback), so resolving can start without a running player.
        self._set_resolving(True)
        # A previous torrent may still be polling stats into the card; silence it
        # so resolve-progress and "Starting mpv…" messages aren't overwritten.
        self._stop_torrent_stats()

        def on_progress(msg: str) -> None:
            GLib.idle_add(self._on_resolve_progress, msg)

        def worker():
            try:
                source = resolve(text, on_progress=on_progress)
            except FileNotFoundError as exc:
                # A local file is gone. If it's a stranded torrent-cache file we
                # can re-fetch it via the torrent that produced it — but that
                # lookup touches the history DB, which is main-thread-only, so
                # hop back over there to decide. `recovering` stops a recovered
                # play that still fails from looping.
                if recovering:
                    GLib.idle_add(self._on_resolve_failed, str(exc))
                else:
                    GLib.idle_add(
                        self._try_recover_cache_miss, exc.filename or text, str(exc)
                    )
                return
            except Exception as exc:
                log.warning("resolve raised for %r: %s", text, exc)
                GLib.idle_add(self._on_resolve_failed, str(exc))
                return
            GLib.idle_add(self._on_resolved, source)

        threading.Thread(target=worker, name="resolver", daemon=True).start()

    def _try_recover_cache_miss(self, missing: str, original_error: str) -> bool:
        """Re-fetch a deleted torrent-cache file via its torrent (main thread).

        A finished torrent can be played straight from the cache as a local
        file; clearing the cache then leaves that history entry pointing at
        nothing. Map the missing path back to the magnet (or .torrent) that
        downloaded it and resolve that instead, which re-acquires the file.
        Falls back to the original "no such file" error when there's no torrent
        source to recover from.
        """
        recovery: str | None = None
        from nab.torrent import is_available
        if is_available():
            from nab.torrent.config import TorrentConfig
            cache_dir = TorrentConfig.load().effective_cache_dir
            recovery = torrent_for_cache_path(
                missing, cache_dir, self.history.torrent_inputs()
            )
        if recovery is None:
            self._on_resolve_failed(original_error)
            return False
        log.info("cache miss for %s — re-fetching via torrent", missing)
        self._on_resolve_progress("File missing from cache — re-fetching from torrent…")
        self._resolve_async(recovery, recovering=True)
        return False

    def _set_resolving(self, busy: bool) -> None:
        self.input_entry.set_sensitive(not busy)
        self.play_button.set_visible(not busy)
        self.input_spinner.set_visible(busy)
        if busy:
            self.input_spinner.start()
        else:
            self.input_spinner.stop()

    def _on_resolve_progress(self, msg: str) -> bool:
        self.video_status_page.set_title("Nabbing…")
        self.video_status_page.set_description(msg)
        return False

    def _on_resolve_failed(self, msg: str) -> bool:
        log.warning("resolve failed: %s", msg)
        self._set_resolving(False)
        self._show_toast(f"Couldn't nab that: {msg}", timeout=5)
        return False

    def _show_toast(self, text: str, timeout: int = 4) -> None:
        toast = Adw.Toast.new(text)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    def _on_resolved(self, source: ResolvedSource) -> bool:
        self._set_resolving(False)
        if not source.source_type.is_torrent:
            self._teardown_torrent(full=False)
        self._current_source = source
        try:
            self._current_entry_id = self.history.upsert_play(
                input=source.original_input,
                source_type=source.source_type.value,
                title=source.title,
                duration=source.duration,
            )
        except Exception:
            log.exception("history upsert failed")
            self._current_entry_id = None

        title = source.title or source.original_input
        self.set_title(f"Nab — {title}")
        self.video_status_page.set_title(title)
        self.video_status_page.set_description("Starting mpv…")

        # Metadata-only UI; safe to run before mpv is up.
        self.subtitle_panel.on_source(source)
        self.series_nav.update(source)
        self.history_sidebar.refresh()

        # Hand off to mpv, spawning its window first if there isn't one.
        self._begin_playback(source)
        return False

    def _begin_playback(self, source: ResolvedSource) -> None:
        """Play `source` in mpv, spawning mpv first if its window isn't open.

        mpv starts lazily and is re-spawned after the user closes its window,
        so the first play — and any play after a Stop — has to wait for a fresh
        process. If mpv is already up we load immediately; otherwise we stash
        the source and `_on_mpv_ready` loads it once the socket is live.
        """
        if self.player is not None and self.player.running:
            self._loadfile_now(source)
            return
        self._pending_source = source
        if self._mpv_starting:
            # A spawn is already in flight; it'll pick up _pending_source.
            return
        # Clear any dead session now so its late disconnect (which would null
        # self.player and clobber this play's state) is recognised as stale.
        self._teardown_mpv()
        self._mpv_starting = True
        self._start_mpv_async()

    def _loadfile_now(self, source: ResolvedSource) -> None:
        player = self.player
        if player is None:
            return

        # Resolve resume position before sending loadfile so mpv applies it
        # atomically as part of opening the file. A follow-up seek would race
        # against file-loaded — mpv silently drops time-pos as "property
        # unavailable" until the demuxer has the file open, which on slow
        # sources (large MKVs, torrent streams) can be hundreds of ms after
        # the loadfile ack.
        resume_pos: float | None = None
        if self._current_entry_id is not None:
            entry = self.history.get(self._current_entry_id)
            if entry is not None and entry.last_position > _RESUME_MIN_SECONDS and not entry.completed:
                resume_pos = entry.last_position

        log.info(
            "loadfile -> %s (source_type=%s, start=%s)",
            source.playable_url, source.source_type.value, resume_pos,
        )
        player.loadfile(source.playable_url, start=resume_pos)
        # mpv preserves `pause` across loadfile; explicitly resume so opening
        # a new file from a paused state plays it.
        player.set_property("pause", False)
        self.video_status_page.set_description(
            f"Source: {source.source_type.value}. Playing in mpv's window."
        )
        # Torrents keep downloading in the background while mpv plays, so show
        # the live transfer dashboard; everything else hides it.
        self._stop_torrent_stats()
        if source.source_type.is_torrent:
            self.torrent_stats.reset()
            self.torrent_stats.widget.set_visible(True)
            self._start_torrent_stats()

        self.play_pause_button.set_sensitive(True)
        self.seek_scale.set_sensitive(True)
        self._is_paused = False
        self._update_play_pause_icon()

        # Schedule periodic position saves while we're playing.
        if self._save_position_source is None:
            self._save_position_source = GLib.timeout_add_seconds(
                _POSITION_SAVE_INTERVAL, self._save_position_tick
            )

        # Subtitle discovery attaches tracks over the live IPC, so it belongs
        # here (mpv guaranteed up), not in _on_resolved.
        self.subtitle_panel.start_discovery(source)

    # ----------------------------------------------------------- torrent stats

    def _start_torrent_stats(self) -> None:
        """Poll the torrent engine and refresh the now-playing card."""
        if self._torrent_stats_source is None:
            self._torrent_stats_source = GLib.timeout_add_seconds(
                _TORRENT_STATS_INTERVAL, self._torrent_stats_tick
            )

    def _stop_torrent_stats(self) -> None:
        if self._torrent_stats_source is not None:
            GLib.source_remove(self._torrent_stats_source)
            self._torrent_stats_source = None
        self.torrent_stats.widget.set_visible(False)

    def _torrent_stats_tick(self) -> bool:
        st = _engine_status()
        if st is None:
            # Torrent gone (stopped/cleaned up) — drop the dashboard and lapse.
            self._torrent_stats_source = None
            self.torrent_stats.widget.set_visible(False)
            return False
        self.torrent_stats.update(st)
        return True

    # ------------------------------------------------------------- mpv events

    def _observe_main(
        self,
        player: Player,
        name: str,
        apply: Callable[[Any], bool],
        cast: Callable[[Any], Any],
        *,
        skip_none: bool = True,
    ) -> None:
        """Observe an mpv property, marshalling updates onto the GTK main loop.

        mpv delivers property changes on its IPC thread; `apply` must run on
        the main thread. `cast` normalises the raw value (float/bool/str);
        `skip_none=True` drops "property unavailable" (None) updates.
        """
        def on_change(value: Any) -> None:
            if value is None and skip_none:
                return
            GLib.idle_add(apply, cast(value))

        player.observe_property(name, on_change)

    def _apply_time_pos(self, value: float) -> bool:
        self._position = value
        self.position_label.set_text(_format_time(value))
        if self._duration and self._duration > 0:
            self.seek_adjustment.set_upper(self._duration)
        # Push the new position into the slider without making the handler
        # round-trip it back to mpv as a "user seek".
        with self._seek_guard:
            self.seek_adjustment.set_value(value)
        return False

    def _apply_duration(self, value: float) -> bool:
        self._duration = value
        self.duration_label.set_text(_format_time(value))
        # set_upper can clamp the value and re-fire value-changed; treat that
        # as code-driven, not a user seek.
        with self._seek_guard:
            self.seek_adjustment.set_upper(max(value, 1.0))
        return False

    def _apply_pause(self, value: bool) -> bool:
        self._is_paused = value
        self._update_play_pause_icon()
        return False

    def _apply_volume(self, value: float) -> bool:
        # Clamp to slider range; mpv allows >100 with --volume-max.
        upper = self.volume_adjustment.get_upper()
        clamped = max(0.0, min(value, upper))
        # Push without re-firing the user-changed path back at mpv.
        with self._volume_guard:
            self.volume_adjustment.set_value(clamped)
        return False

    def _apply_media_title(self, title: str) -> bool:
        # If yt-dlp didn't give us a title, mpv may eventually produce one.
        if self._current_source and not self._current_source.title:
            self.set_title(f"Nab — {title}")
            self.video_status_page.set_title(title)
        return False

    def _on_end_file(self, data: dict) -> None:
        reason = data.get("reason")
        log.info("end-file reason=%s", reason)
        if reason == "eof":
            GLib.idle_add(self._save_position_now, True)

    # ---------------------------------------------------- transport / volume

    def _on_play_pause_clicked(self, _b) -> None:
        if self.player is None:
            return
        # Toggle on mpv's side. Computing `not self._is_paused` here races with
        # the property-change event: a click before the previous state has
        # echoed back uses stale state and can pin mpv into a paused state
        # (we send pause=True against an already-paused mpv → no event → loop).
        self.player.command("cycle", "pause")

    def _update_play_pause_icon(self) -> None:
        icon = "media-playback-start-symbolic" if self._is_paused else "media-playback-pause-symbolic"
        self.play_pause_button.set_icon_name(icon)

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        # mpv echoing a volume back travels through _apply_volume under the
        # guard, so this only runs for genuine user changes. We still persist
        # even when no player is up, so a pre-playback tweak survives.
        if self._volume_guard:
            return
        if self.player is not None:
            self.player.set_property("volume", scale.get_value())
        self._schedule_volume_save(scale.get_value())

    def _schedule_volume_save(self, volume: float) -> None:
        if self._volume_save_source is not None:
            GLib.source_remove(self._volume_save_source)
        self._volume_save_source = GLib.timeout_add(
            _VOLUME_SAVE_DELAY_MS, self._save_volume_now, volume
        )

    def _save_volume_now(self, volume: float) -> bool:
        self._volume_save_source = None
        self._playback_config.volume = volume
        try:
            self._playback_config.save()
        except OSError:
            log.exception("failed to persist volume")
        return False

    def _on_seek_value_changed(self, adjustment: Gtk.Adjustment) -> None:
        if self._seek_guard or self.player is None:
            return
        target = adjustment.get_value()
        self.player.set_property("time-pos", target)

    # ----------------------------------------------------- history & position

    def _save_position_tick(self) -> bool:
        self._save_position_now(False)
        return True

    def _save_position_now(self, completed_hint: bool) -> bool:
        if self._current_entry_id is None:
            return False
        duration = self._duration
        if completed_hint and duration:
            position = duration
        else:
            position = self._position
        try:
            self.history.update_position(self._current_entry_id, position, duration)
        except Exception:
            log.exception("history update_position failed")
        return False

    def _on_history_activate(self, input_str: str) -> None:
        self.play_input(input_str)
        self.split.set_show_sidebar(False)
