"""Transport-bar button + logic for (re-)fetching subtitles.

Discovery runs on a worker thread (subliminal network calls) and attaches
any found tracks to the live mpv over IPC. The window supplies the current
player and a toast sink; the rest of the state lives here.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from nab.resolver import ResolvedSource, SourceType  # noqa: E402
from nab.subtitles import (  # noqa: E402
    SubtitlesConfig,
    discover_for_source,
    is_available,
    sync_to_release,
)
from nab.subtitles.bundled import await_bundled_subtitles  # noqa: E402
from nab.subtitles.discover import DISCOVERABLE_SOURCE_TYPES  # noqa: E402

if TYPE_CHECKING:
    from nab.player import Player

log = logging.getLogger(__name__)

_SUBS_UNAVAILABLE_MSG = "Subtitle discovery disabled — `subliminal` not installed"

# How long to give a torrent's own sidecars before falling back to scraping.
# Only torrents that actually ship subtitles ever wait.
_BUNDLED_TIMEOUT = 20.0


class SubtitlePanel:
    """Owns the re-fetch-subtitles button + spinner and discovery orchestration."""

    def __init__(
        self,
        config: SubtitlesConfig,
        *,
        get_player: "Callable[[], Player | None]",
        show_toast: Callable[..., None],
    ) -> None:
        self._config = config
        self._get_player = get_player
        self._show_toast = show_toast
        self._current_source: ResolvedSource | None = None
        self._cancel: threading.Event | None = None
        self._unavailable_warned = False
        self._build_widgets()

    def _build_widgets(self) -> None:
        # Force-download subtitles. Shown for sources we can actually search
        # (see DISCOVERABLE_SOURCE_TYPES) when subliminal is available; the
        # auto path already skips when subtitles are present.
        self.button = Gtk.Button.new_from_icon_name("media-view-subtitles-symbolic")
        self.button.set_tooltip_text("Re-fetch subtitles")
        self.button.set_visible(False)
        self.button.connect("clicked", self._on_force_clicked)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)

    @property
    def bar_widgets(self) -> tuple[Gtk.Widget, ...]:
        """Widgets to append to the transport bar, in order."""
        return (self.button, self.spinner)

    def on_source(self, source: ResolvedSource) -> None:
        """Reset button state for a new source (safe before mpv is up)."""
        self._current_source = source
        # New file: reset any leftover busy state from a previous force-download.
        self.button.set_sensitive(True)
        self.spinner.stop()
        self.spinner.set_visible(False)
        show = source.source_type in DISCOVERABLE_SOURCE_TYPES and is_available()
        self.button.set_visible(show)

    def start_discovery(self, source: ResolvedSource, *, force: bool = False) -> None:
        if not force and not self._config.enabled:
            return
        if source.source_type not in DISCOVERABLE_SOURCE_TYPES:
            return

        if not is_available():
            # Always toast on an explicit force; for auto-discovery warn once.
            if force:
                self._show_toast(_SUBS_UNAVAILABLE_MSG, timeout=6)
            elif not self._unavailable_warned:
                self._unavailable_warned = True
                self._show_toast(_SUBS_UNAVAILABLE_MSG, timeout=6)
            return

        # Cancel any in-flight discovery from the previous file. Subliminal's
        # network call can't be interrupted; the worker just discards its
        # result when it sees the token set.
        if self._cancel is not None:
            self._cancel.set()
        cancel = threading.Event()
        self._cancel = cancel
        player = self._get_player()  # captured for the worker; commands are thread-safe

        if force:
            self._set_busy(True)

        def worker():
            try:
                paths = self._collect(source, cancel, force=force)
            except Exception:
                log.exception("subtitle discovery raised")
                paths = []
            # Cancelled by a new file load — new file's visibility update has
            # already cleared the busy state, so just bail without a toast.
            if cancel.is_set():
                return
            if player is None:
                if force:
                    GLib.idle_add(self._on_force_done, 0)
                return

            added = 0
            for i, path in enumerate(paths):
                if cancel.is_set():
                    return
                try:
                    # Synchronous so mpv errors raise instead of vanishing.
                    # On force, always select the first new track so the user
                    # immediately sees the result of clicking the button.
                    player.add_subtitle(str(path), select=(i == 0))
                    log.info("sub-add ok: %s (select=%s)", path, i == 0)
                    added += 1
                except Exception:
                    log.exception("mpv sub-add failed for %s", path)

            if cancel.is_set():
                return
            if force:
                GLib.idle_add(self._on_force_done, added)
            elif added:
                GLib.idle_add(self._on_added, added)

        threading.Thread(target=worker, name="subtitles-discover", daemon=True).start()

    def _collect(
        self, source: ResolvedSource, cancel: threading.Event, *, force: bool
    ) -> list[Path]:
        """Get subtitle files for `source`, preferring the ones it ships with.

        A magnet's own sidecars beat scraped ones on sync, so the auto path
        waits for them first and only falls back to the providers when the
        torrent has none. Forcing skips straight to the providers — the
        bundled subs are already attached by then, and "re-fetch" means the
        user wants something other than what they're looking at.

        Anything scraped is retimed against the release before it goes to mpv;
        bundled sidecars come back untouched, being the timing everything else
        is measured against.
        """
        if not force and source.source_type is SourceType.MAGNET:
            bundled = await_bundled_subtitles(_BUNDLED_TIMEOUT, cancel)
            if bundled:
                log.info("using %d subtitle(s) bundled in the torrent", len(bundled))
                return bundled

        paths = discover_for_source(source, self._config, cancel, force=force)
        if not paths or not self._config.sync or cancel.is_set():
            return paths
        return sync_to_release(paths, source, self._config.languages)

    def _set_busy(self, busy: bool) -> None:
        self.button.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _on_force_clicked(self, _b: Gtk.Button) -> None:
        if self._current_source is None:
            return
        self.start_discovery(self._current_source, force=True)

    def _on_force_done(self, count: int) -> bool:
        self._set_busy(False)
        if count:
            self._show_toast(
                f"Subtitles re-fetched ({count} track{'s' if count > 1 else ''})"
            )
        else:
            self._show_toast("No subtitles found")
        return False

    def _on_added(self, count: int) -> bool:
        self._show_toast(f"Subtitles loaded ({count} track{'s' if count > 1 else ''})")
        return False
