import logging
import sqlite3
from pathlib import Path

from nab.history.model import HistoryEntry
from nab.paths import data_home

log = logging.getLogger(__name__)

# Fraction of the runtime past which a play counts as "completed".
_COMPLETION_RATIO = 0.9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT,
    input           TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    duration        REAL,
    last_position   REAL DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    nabbed_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_played_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    play_count      INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_history_last_played
    ON history(last_played_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_input
    ON history(input);
"""


def default_db_path() -> Path:
    p = data_home() / "history.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_COLUMNS = """
    id, title, input, source_type, duration, last_position,
    completed, nabbed_at, last_played_at, play_count
"""


def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"],
        title=row["title"],
        input=row["input"],
        source_type=row["source_type"],
        duration=row["duration"],
        last_position=row["last_position"] or 0.0,
        completed=bool(row["completed"]),
        nabbed_at=row["nabbed_at"],
        last_played_at=row["last_played_at"],
        play_count=row["play_count"],
    )


class HistoryDatabase:
    """SQLite-backed watch history. All access is on the GTK main thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        log.info("history db at %s", self.path)

    def upsert_play(
        self,
        *,
        input: str,
        source_type: str,
        title: str | None,
        duration: float | None,
    ) -> int:
        """Record that we just started playing this input. Returns row id."""
        cur = self._conn.execute(
            """
            INSERT INTO history (input, source_type, title, duration)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(input) DO UPDATE SET
                play_count = play_count + 1,
                last_played_at = CURRENT_TIMESTAMP,
                title = COALESCE(excluded.title, history.title),
                duration = COALESCE(excluded.duration, history.duration),
                source_type = excluded.source_type
            RETURNING id
            """,
            (input, source_type, title, duration),
        )
        row = cur.fetchone()
        return int(row["id"])

    def update_position(self, entry_id: int, position: float, duration: float | None) -> None:
        completed = 0
        if duration and duration > 0 and position / duration > _COMPLETION_RATIO:
            completed = 1
        self._conn.execute(
            """
            UPDATE history
            SET last_position = ?,
                duration = COALESCE(?, duration),
                completed = MAX(completed, ?)
            WHERE id = ?
            """,
            (position, duration, completed, entry_id),
        )

    def get(self, entry_id: int) -> HistoryEntry | None:
        """Fetch a single history entry by id, or None if it's gone."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM history WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def magnet_inputs(self) -> list[str]:
        """Every magnet input, most-recently-played first.

        Used to recover a deleted torrent-cache file: the orphaned cache path
        maps back to the magnet that re-downloads it (see magnet_for_cache_path).
        """
        rows = self._conn.execute(
            """
            SELECT input FROM history
            WHERE source_type = 'magnet'
            ORDER BY last_played_at DESC
            """
        ).fetchall()
        return [r["input"] for r in rows]

    def list_recent(self, limit: int = 50) -> list[HistoryEntry]:
        rows = self._conn.execute(
            f"""
            SELECT {_COLUMNS}
            FROM history
            ORDER BY last_played_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]
