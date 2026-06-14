import logging
from dataclasses import dataclass, field
from typing import Self

from nab.config import load_config

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SubtitlesConfig:
    """User preferences for automatic subtitle discovery.

    Loaded from `$XDG_CONFIG_HOME/nab/config.toml`. Example:

        [subtitles]
        enabled = true
        languages = ["en", "cs"]
        save_next_to_video = true

        [subtitles.opensubtitles]
        username = "..."
        password = "..."
    """

    enabled: bool = True
    languages: list[str] = field(default_factory=lambda: ["en"])
    save_next_to_video: bool = True
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None

    @classmethod
    def load(cls) -> Self:
        data = load_config()
        sub = data.get("subtitles") or {}
        os_sub = sub.get("opensubtitles") or {}
        languages = sub.get("languages")
        if not isinstance(languages, list) or not languages:
            languages = ["en"]
        return cls(
            enabled=bool(sub.get("enabled", True)),
            languages=[str(x) for x in languages],
            save_next_to_video=bool(sub.get("save_next_to_video", True)),
            opensubtitles_username=os_sub.get("username"),
            opensubtitles_password=os_sub.get("password"),
        )
