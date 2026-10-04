from dataclasses import dataclass
from typing import Self

from nab.config import load_config, save_config

# Volume mpv starts at the first time Nab runs, before the user has touched
# the slider. On the 0-100 scale mpv uses by default.
DEFAULT_VOLUME = 80.0


@dataclass(slots=True)
class PlaybackConfig:
    """Playback preferences persisted in config.toml's [playback] table.

    Example:

        [playback]
        volume = 65.0
    """

    volume: float | None = None

    @classmethod
    def load(cls) -> Self:
        data = load_config()
        playback = data.get("playback") or {}
        volume = playback.get("volume")
        return cls(volume=float(volume) if isinstance(volume, (int, float)) else None)

    def save(self) -> None:
        data = load_config()
        playback = data.setdefault("playback", {})
        if self.volume is not None:
            playback["volume"] = self.volume
        else:
            playback.pop("volume", None)
        if not playback:
            data.pop("playback", None)
        save_config(data)

    @property
    def effective_volume(self) -> float:
        if self.volume is not None and self.volume >= 0:
            return self.volume
        return DEFAULT_VOLUME
