import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from nab.gtkutil import log_dialog_error  # noqa: E402
from nab.torrent.config import (  # noqa: E402
    DEFAULT_DOWNLOAD_AHEAD,
    DEFAULT_METADATA_TIMEOUT,
    TorrentConfig,
    default_cache_dir,
)


class NabPreferencesWindow(Adw.PreferencesWindow):
    __gtype_name__ = "NabPreferencesWindow"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Preferences")
        self.set_default_size(480, -1)

        self._torrent_config = TorrentConfig.load()
        self._build_torrent_page()

    def _build_torrent_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Torrents")
        page.set_icon_name("folder-download-symbolic")
        self.add(page)

        group = Adw.PreferencesGroup()
        group.set_title("Storage")
        page.add(group)

        self._cache_row = Adw.ActionRow()
        self._cache_row.set_title("Cache directory")
        self._cache_row.set_subtitle(str(self._torrent_config.effective_cache_dir))
        self._cache_row.set_subtitle_lines(1)

        browse_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.set_tooltip_text("Choose directory")
        browse_btn.connect("clicked", self._on_browse)
        self._cache_row.add_suffix(browse_btn)

        reset_btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.set_tooltip_text("Reset to default")
        reset_btn.connect("clicked", self._on_reset)
        self._cache_row.add_suffix(reset_btn)

        group.add(self._cache_row)

        network = Adw.PreferencesGroup()
        network.set_title("Network")
        page.add(network)

        self._timeout_row = Adw.SpinRow.new_with_range(30, 600, 10)
        self._timeout_row.set_title("Metadata timeout")
        self._timeout_row.set_subtitle(
            "Seconds to wait for a magnet's metadata before giving up"
        )
        self._timeout_row.set_digits(0)
        # Set the value before connecting so the initial sync doesn't save.
        self._timeout_row.set_value(self._torrent_config.effective_metadata_timeout)
        self._timeout_row.connect("notify::value", self._on_timeout_changed)
        network.add(self._timeout_row)

        downloads = Adw.PreferencesGroup()
        downloads.set_title("Downloads")
        page.add(downloads)

        self._ahead_row = Adw.SpinRow.new_with_range(0, 10, 1)
        self._ahead_row.set_title("Download ahead")
        self._ahead_row.set_subtitle(
            "Upcoming episodes to download in the background while watching "
            "(0 to disable)"
        )
        self._ahead_row.set_digits(0)
        # Set the value before connecting so the initial sync doesn't save.
        self._ahead_row.set_value(self._torrent_config.effective_download_ahead)
        self._ahead_row.connect("notify::value", self._on_ahead_changed)
        downloads.add(self._ahead_row)

    def _on_browse(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose torrent cache directory")
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error as exc:
            log_dialog_error(exc, "folder dialog")
            return
        path = gfile.get_path() if gfile else None
        if path:
            self._torrent_config.cache_dir = path
            self._torrent_config.save()
            self._cache_row.set_subtitle(path)

    def _on_reset(self, _button: Gtk.Button) -> None:
        self._torrent_config.cache_dir = None
        self._torrent_config.save()
        self._cache_row.set_subtitle(str(default_cache_dir()))

    def _on_timeout_changed(self, row: "Adw.SpinRow", _pspec) -> None:
        value = int(row.get_value())
        # Persist None when it matches the default so future default bumps
        # still reach users who never customised this.
        self._torrent_config.metadata_timeout = (
            None if value == DEFAULT_METADATA_TIMEOUT else value
        )
        self._torrent_config.save()

    def _on_ahead_changed(self, row: "Adw.SpinRow", _pspec) -> None:
        value = int(row.get_value())
        # Persist None at the default so a future default bump still reaches
        # users who never touched this.
        self._torrent_config.download_ahead = (
            None if value == DEFAULT_DOWNLOAD_AHEAD else value
        )
        self._torrent_config.save()
