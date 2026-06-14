"""Centralized XDG Base Directory paths for the nab application."""

import os
from pathlib import Path

_APP = "nab"


def _xdg(var: str, default: Path) -> Path:
    """App-scoped XDG dir: ``$<var>/nab`` or ``<default>/nab``."""
    base = os.environ.get(var)
    return (Path(base) if base else default) / _APP


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")


def runtime_dir() -> Path:
    # Unlike the others this is the bare base dir, not app-scoped: callers
    # name their own files (e.g. nab-mpv-<pid>.sock) rather than nest a subdir.
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) if base else Path("/tmp")
