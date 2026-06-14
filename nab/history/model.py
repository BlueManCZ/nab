from dataclasses import dataclass


@dataclass(slots=True)
class HistoryEntry:
    id: int | None
    title: str | None
    input: str
    source_type: str
    duration: float | None
    last_position: float
    completed: bool
    nabbed_at: str
    last_played_at: str
    play_count: int
