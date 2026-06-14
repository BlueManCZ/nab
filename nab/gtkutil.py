"""Small shared helpers for GTK file dialogs."""

import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

log = logging.getLogger(__name__)


def log_dialog_error(exc: GLib.Error, what: str) -> None:
    """Log a file-dialog GError, staying quiet when the user just dismissed it."""
    if exc.code != Gtk.DialogError.DISMISSED:
        log.warning("%s failed: %s", what, exc.message)
