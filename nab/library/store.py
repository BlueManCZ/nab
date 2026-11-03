"""Persisted record of the torrents in the library.

Each torrent the user nabs is remembered with its name and video file list,
so the library can be browsed — and any episode picked — without libtorrent
or the swarm. Lives in the history database file, alongside the per-episode
progress it's read together with; all access is on the GTK main thread.
"""

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nab.history.database import default_db_path
from nab.history.model import HistoryEntry
from nab.resolver.source import local_path, magnet_info_hash, split_torrent_fragment
from nab.torrent.model import TorrentFile, TorrentInfo

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_torrents (
    info_hash   TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    name        TEXT NOT NULL,
    hidden      INTEGER NOT NULL DEFAULT 0,
    added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS library_files (
    info_hash   TEXT NOT NULL
                REFERENCES library_torrents(info_hash) ON DELETE CASCADE,
    file_index  INTEGER NOT NULL,
    sub_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    PRIMARY KEY (info_hash, file_index)
);

-- Every source string a torrent has been opened by. A torrent's identity is
-- its info-hash, but history rows are keyed by the source the user handed us
-- (one magnet may come with different tracker lists, a .torrent may move), so
-- this maps each of them back.
CREATE TABLE IF NOT EXISTS library_sources (
    source      TEXT PRIMARY KEY,
    info_hash   TEXT NOT NULL
);
"""


@dataclass(slots=True, frozen=True)
class StoredTorrent:
    info_hash: str
    source: str        # the torrent source to replay, without a #file= fragment
    name: str
    added_at: str
    files: tuple[TorrentFile, ...]


# History progress, keyed by (info_hash, sub_path) of the file it belongs to.
ProgressIndex = dict[tuple[str, str], HistoryEntry]


class LibraryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._conn = sqlite3.connect(path or default_db_path(), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def add(self, source: str, info: TorrentInfo, *, reveal: bool = True) -> None:
        """Remember the torrent `source` opened to.

        `reveal` brings a torrent the user removed back onto the shelf — right
        when they play it again, wrong for a background backfill.
        """
        if info.info_hash is None or not info.video_files:
            return
        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute(
                """
                INSERT INTO library_torrents (info_hash, source, name)
                VALUES (?, ?, ?)
                ON CONFLICT(info_hash) DO UPDATE SET
                    source = excluded.source,
                    name = excluded.name,
                    hidden = CASE WHEN ? THEN 0 ELSE hidden END
                """,
                (info.info_hash, source, info.name or info.video_files[0].name, reveal),
            )
            self._conn.execute(
                "DELETE FROM library_files WHERE info_hash = ?", (info.info_hash,)
            )
            self._conn.executemany(
                """
                INSERT INTO library_files (info_hash, file_index, sub_path, size)
                VALUES (?, ?, ?, ?)
                """,
                [(info.info_hash, f.index, f.sub_path, f.size) for f in info.video_files],
            )
            self._conn.execute(
                """
                INSERT INTO library_sources (source, info_hash) VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET info_hash = excluded.info_hash
                """,
                (source, info.info_hash),
            )

    def set_hidden(self, info_hashes: Iterable[str], hidden: bool) -> None:
        """Take torrents off the library shelf (or put them back).

        Hidden rather than deleted, so a startup backfill doesn't resurrect
        what the user removed. Playing the torrent again un-hides it.
        """
        self._conn.executemany(
            "UPDATE library_torrents SET hidden = ? WHERE info_hash = ?",
            [(int(hidden), h) for h in info_hashes],
        )

    def known_sources(self) -> set[str]:
        """Every source already recorded, hidden torrents included."""
        rows = self._conn.execute("SELECT source FROM library_sources").fetchall()
        return {r["source"] for r in rows}

    def torrents(self) -> list[StoredTorrent]:
        """The visible library, with each torrent's video files."""
        files: dict[str, list[TorrentFile]] = {}
        for r in self._conn.execute(
            "SELECT info_hash, file_index, sub_path, size FROM library_files "
            "ORDER BY sub_path"
        ):
            files.setdefault(r["info_hash"], []).append(
                TorrentFile(index=r["file_index"], sub_path=r["sub_path"], size=r["size"])
            )
        rows = self._conn.execute(
            "SELECT info_hash, source, name, added_at FROM library_torrents "
            "WHERE hidden = 0"
        ).fetchall()
        return [
            StoredTorrent(
                info_hash=r["info_hash"],
                source=r["source"],
                name=r["name"],
                added_at=r["added_at"],
                files=tuple(files.get(r["info_hash"], ())),
            )
            for r in rows
        ]

    def progress_index(
        self,
        torrent_entries: Iterable[HistoryEntry],
        local_entries: Iterable[HistoryEntry] = (),
        cache_dir: Path | None = None,
    ) -> ProgressIndex:
        """Map history rows onto the torrent files they recorded progress for.

        Torrent rows map through the source they were played by. A finished
        download can also be opened straight from the torrent cache as a local
        file; `local_entries` under `cache_dir` count for every torrent file
        stored at that path. When one file has several rows (a magnet
        re-pasted with other trackers, a cache-file replay), the most recently
        played one wins.
        """
        index: ProgressIndex = {}

        def offer(key: tuple[str, str], entry: HistoryEntry) -> None:
            held = index.get(key)
            if held is None or entry.last_played_at > held.last_played_at:
                index[key] = entry

        aliases = {
            r["source"]: r["info_hash"]
            for r in self._conn.execute("SELECT source, info_hash FROM library_sources")
        }
        for entry in torrent_entries:
            base, sub_path = split_torrent_fragment(entry.input)
            if sub_path is None:
                continue
            info_hash = aliases.get(base) or magnet_info_hash(base)
            if info_hash is not None:
                offer((info_hash, sub_path), entry)

        if cache_dir is not None:
            owners: dict[str, list[str]] = {}
            for r in self._conn.execute("SELECT info_hash, sub_path FROM library_files"):
                owners.setdefault(r["sub_path"], []).append(r["info_hash"])
            root = cache_dir.resolve()
            for entry in local_entries:
                try:
                    sub_path = str(local_path(entry.input).relative_to(root))
                except ValueError:
                    continue  # not a torrent-cache file
                for info_hash in owners.get(sub_path, ()):
                    offer((info_hash, sub_path), entry)
        return index
