"""Torrent streaming engine.

Requires python-libtorrent (system package, not on PyPI).
Install via system package manager, e.g.:
  Gentoo: emerge net-libs/libtorrent-rasterbar (with python USE flag)
  Arch:   pacman -S libtorrent-rasterbar
  Debian: apt install python3-libtorrent
"""

try:
    import libtorrent as _lt  # noqa: F401

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def is_available() -> bool:
    return _AVAILABLE


__all__ = ["is_available"]
