import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from nab.gtkutil import log_dialog_error  # noqa: E402
from nab.torrent.config import TorrentConfig, default_cache_dir  # noqa: E402


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
