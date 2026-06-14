"""Shared configuration utilities for reading and writing config.toml."""

import logging
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from nab.paths import config_home

log = logging.getLogger(__name__)


def config_path() -> Path:
    return config_home() / "config.toml"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        log.exception("failed to parse %s; using defaults", path)
        return {}


def save_config(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(data, f)
