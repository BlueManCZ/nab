import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from nab import APP_ID, __version__  # noqa: E402
from nab.preferences import NabPreferencesWindow  # noqa: E402
from nab.window import NabWindow  # noqa: E402

log = logging.getLogger(__name__)


class NabApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._window: NabWindow | None = None

        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._on_preferences)
        self.add_action(prefs_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

    def do_activate(self) -> None:  # type: ignore[override]
        if self._window is None:
            self._window = NabWindow(application=self)
        self._window.present()

    def do_open(self, files, n_files, hint):  # type: ignore[override]
        self.do_activate()
        if files and self._window is not None:
            gfile = files[0]
            target = gfile.get_path() or gfile.get_uri()
            # GIO normalises `magnet:?xt=...` into `magnet:///?xt=...` when
            # wrapping it in a GFile (it inserts an empty authority). The
            # resolver matches the standard `magnet:?` form, so undo it here.
            if target and target.startswith("magnet:///"):
                target = "magnet:" + target[len("magnet:///"):]
            self._window.queue_initial_play(target)

    def _on_preferences(self, _action, _param) -> None:
        win = NabPreferencesWindow(transient_for=self._window)
        win.present()

    def _on_about(self, _action, _param) -> None:
        about = Adw.AboutDialog(
            application_name="Nab",
            application_icon=APP_ID,
            version=__version__,
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self._window)

    def do_shutdown(self) -> None:  # type: ignore[override]
        if self._window is not None:
            self._window.shutdown_player()
        Adw.Application.do_shutdown(self)
