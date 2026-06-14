from dataclasses import dataclass
from pathlib import Path
from typing import Self

from nab.config import load_config, save_config
from nab.paths import cache_home


def default_cache_dir() -> Path:
    return cache_home() / "torrents"


@dataclass(slots=True)
class TorrentConfig:
    cache_dir: str | None = None

    @classmethod
    def load(cls) -> Self:
        data = load_config()
        torrent = data.get("torrent") or {}
        return cls(cache_dir=torrent.get("cache_dir"))

    def save(self) -> None:
        data = load_config()
        torrent = data.setdefault("torrent", {})
        if self.cache_dir:
            torrent["cache_dir"] = self.cache_dir
        else:
            torrent.pop("cache_dir", None)
            if not torrent:
                data.pop("torrent", None)
        save_config(data)

    @property
    def effective_cache_dir(self) -> Path:
        if self.cache_dir:
            return Path(self.cache_dir)
        return default_cache_dir()
