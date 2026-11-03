from dataclasses import dataclass

# Below this many seconds in, a play isn't worth resuming — the user barely
# started it.
RESUME_MIN_SECONDS = 5.0


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

    @property
    def resumable(self) -> bool:
        """True when replaying should pick up where the user left off."""
        return not self.completed and self.last_position > RESUME_MIN_SECONDS
