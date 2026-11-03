"""Torrent metadata as the rest of the app sees it.

Plain dataclasses with no libtorrent dependency, so the resolver can hand
them out on a ``ResolvedSource`` and the library can persist and browse them
whether or not libtorrent is installed.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class TorrentFile:
    """One file inside a torrent, as exposed to the rest of the app."""
    index: int
    sub_path: str    # path inside the torrent, e.g. "Show.S01/E01.mkv"
    size: int

    @property
    def name(self) -> str:
        return Path(self.sub_path).name


@dataclass(slots=True, frozen=True)
class TorrentInfo:
    """Metadata about an open torrent. video_files is sorted by sub_path.

    ``name`` is the torrent's own name (its root directory, or the file
    itself for a single-file torrent). ``subtitle_files`` holds the sidecars
    worth fetching, not every subtitle in the torrent.
    """
    info_hash: str | None
    video_files: tuple[TorrentFile, ...]
    subtitle_files: tuple[TorrentFile, ...] = ()
    name: str = ""

    def by_index(self, index: int) -> TorrentFile | None:
        return next((f for f in self.video_files if f.index == index), None)

    def by_sub_path(self, sub_path: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.sub_path == sub_path), None)

    def by_name(self, name: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.name == name), None)
