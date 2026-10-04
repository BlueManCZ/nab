from dataclasses import dataclass
from pathlib import Path
from typing import Self

from nab.config import load_config, save_config
from nab.paths import cache_home

# Seconds to wait for a magnet's metadata to arrive from the swarm before
# giving up. Poorly-seeded torrents can take a while, so this is generous.
DEFAULT_METADATA_TIMEOUT = 180

# How many upcoming episodes to download in the background while streaming.
# Unset (None) falls back to this; an explicit 0 disables prefetch entirely.
DEFAULT_DOWNLOAD_AHEAD = 1


def default_cache_dir() -> Path:
    return cache_home() / "torrents"


@dataclass(slots=True)
class TorrentConfig:
    cache_dir: str | None = None
    metadata_timeout: int | None = None
    download_ahead: int | None = None

    @classmethod
    def load(cls) -> Self:
        data = load_config()
        torrent = data.get("torrent") or {}
        return cls(
            cache_dir=torrent.get("cache_dir"),
            metadata_timeout=torrent.get("metadata_timeout"),
            download_ahead=torrent.get("download_ahead"),
        )

    def save(self) -> None:
        data = load_config()
        torrent = data.setdefault("torrent", {})
        if self.cache_dir:
            torrent["cache_dir"] = self.cache_dir
        else:
            torrent.pop("cache_dir", None)
        if self.metadata_timeout:
            torrent["metadata_timeout"] = self.metadata_timeout
        else:
            torrent.pop("metadata_timeout", None)
        # Store None as "unset"; keep an explicit 0 so it can mean "disabled"
        # rather than collapsing back to the default on reload.
        if self.download_ahead is not None:
            torrent["download_ahead"] = self.download_ahead
        else:
            torrent.pop("download_ahead", None)
        if not torrent:
            data.pop("torrent", None)
        save_config(data)

    @property
    def effective_cache_dir(self) -> Path:
        if self.cache_dir:
            return Path(self.cache_dir)
        return default_cache_dir()

    @property
    def effective_metadata_timeout(self) -> int:
        if self.metadata_timeout and self.metadata_timeout > 0:
            return self.metadata_timeout
        return DEFAULT_METADATA_TIMEOUT

    @property
    def effective_download_ahead(self) -> int:
        # None means "never set" → use the default; 0 is a real "disabled".
        if self.download_ahead is not None and self.download_ahead >= 0:
            return self.download_ahead
        return DEFAULT_DOWNLOAD_AHEAD
