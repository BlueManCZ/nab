import logging
import threading
from collections.abc import Callable
from functools import cache, wraps
from pathlib import Path

from nab.paths import cache_home
from nab.resolver import ResolvedSource, SourceType
from nab.subtitles.config import SubtitlesConfig

log = logging.getLogger(__name__)

_FREE_PROVIDERS = ["podnapisi", "gestdown", "tvsubtitles"]

_SUBTITLE_EXTS = frozenset({
    ".srt", ".ass", ".ssa", ".vtt", ".webvtt",
    ".sub", ".sbv", ".idx", ".sup",
})


def _once(fn: Callable[[], None]) -> Callable[[], None]:
    """Run `fn` the first time it's called and never again.

    Thread-safe and exactly-once: the lock is held across `fn`, so concurrent
    callers block until the first finishes and observe the side effect rather
    than racing it (subliminal's `region.configure` must not run twice).
    """
    lock = threading.Lock()
    done = False

    @wraps(fn)
    def wrapper() -> None:
        nonlocal done
        with lock:
            if done:
                return
            fn()
            done = True

    return wrapper


def has_existing_subtitles(video_path: Path) -> bool:
    """True if a sidecar subtitle file already exists for `video_path`."""
    stem_prefix = video_path.stem.lower() + "."
    try:
        for sibling in video_path.parent.iterdir():
            if not sibling.is_file():
                continue
            name = sibling.name.lower()
            if not name.startswith(stem_prefix):
                continue
            if sibling.suffix.lower() in _SUBTITLE_EXTS:
                return True
    except OSError:
        return False
    return False


@cache
def is_available() -> bool:
    try:
        import subliminal  # noqa: F401
        import babelfish  # noqa: F401
    except ImportError:
        return False
    return True


@_once
def _warn_missing() -> None:
    log.warning("subtitle discovery disabled — `subliminal` not installed")


@_once
def _configure_cache() -> None:
    """subliminal uses dogpile.cache and refuses to call providers without it."""
    from subliminal import region

    cache_db = cache_home() / "subliminal.dbm"
    try:
        cache_db.parent.mkdir(parents=True, exist_ok=True)
        region.configure(
            "dogpile.cache.dbm",
            arguments={"filename": str(cache_db)},
        )
    except Exception:
        log.exception("dbm cache setup failed; falling back to in-memory")
        try:
            region.configure("dogpile.cache.memory")
        except Exception:
            log.exception("in-memory cache setup also failed")


def _parse_language(code: str):
    """Best-effort parse of a language code to babelfish.Language."""
    from babelfish import Language

    code = code.strip().lower()
    try:
        if len(code) == 2:
            return Language.fromalpha2(code)
        if len(code) == 3:
            return Language(code)
        return Language.fromietf(code)
    except Exception:
        log.warning("unknown language code: %r", code)
        return None


def _subs_cache_dir() -> Path:
    d = cache_home() / "subtitles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def discover_for_source(
    source: ResolvedSource,
    config: SubtitlesConfig,
    cancel_token: threading.Event | None = None,
    *,
    force: bool = False,
) -> list[Path]:
    """Find and download subtitles for `source`. Returns saved file paths.

    Blocking: call from a worker thread. Returns [] on no match, error, or
    unsupported source type.
    """
    if not force and not config.enabled:
        return []
    if source.source_type is not SourceType.LOCAL_FILE:
        return []

    try:
        from subliminal import download_best_subtitles, scan_video
    except ImportError:
        _warn_missing()
        return []

    video_path = Path(source.playable_url)
    if not video_path.exists() or not video_path.is_file():
        return []

    if not force and has_existing_subtitles(video_path):
        log.info("subtitles already present for %s; skipping auto-download", video_path.name)
        return []

    _configure_cache()

    if cancel_token is not None and cancel_token.is_set():
        return []

    try:
        video = scan_video(str(video_path))
    except Exception:
        log.exception("scan_video failed for %s", video_path)
        return []

    languages = {lang for lang in (_parse_language(c) for c in config.languages) if lang}
    if not languages:
        log.warning("no valid languages configured; skipping subtitle discovery")
        return []

    providers = list(_FREE_PROVIDERS)
    provider_configs: dict[str, dict[str, str]] = {}
    if config.opensubtitles_username and config.opensubtitles_password:
        providers.append("opensubtitlescom")
        provider_configs["opensubtitlescom"] = {
            "username": config.opensubtitles_username,
            "password": config.opensubtitles_password,
        }

    if cancel_token is not None and cancel_token.is_set():
        return []

    try:
        result = download_best_subtitles(
            {video},
            languages,
            providers=providers,
            provider_configs=provider_configs,
        )
    except Exception:
        log.exception("subliminal download failed for %s", video_path)
        return []

    found = result.get(video, [])
    if not found:
        log.info("no subtitles found for %s", video_path.name)
        return []

    if cancel_token is not None and cancel_token.is_set():
        return []

    # Save next to the video by default so mpv auto-loads the sidecar on
    # replay; if that directory isn't writable, fall back to the cache dir.
    save_dir: Path | None = None if config.save_next_to_video else _subs_cache_dir()
    saved = _save_subtitles(video, found, save_dir)
    if not saved and save_dir is None:
        log.info("subtitle save next to video failed; retrying in cache dir")
        save_dir = _subs_cache_dir()
        saved = _save_subtitles(video, found, save_dir)

    return _resolve_saved_paths(video, saved, save_dir)


def _save_subtitles(video, subtitles, directory: Path | None):
    """Write `subtitles` to `directory`, or next to the video when None.

    Returns the list subliminal reports as saved, or [] if the target isn't
    writable.
    """
    from subliminal import save_subtitles

    try:
        if directory is None:
            return save_subtitles(video, subtitles)
        return save_subtitles(video, subtitles, directory=str(directory))
    except (PermissionError, OSError) as exc:
        log.warning("subtitle save failed (%s)", exc)
        return []
    except Exception:
        log.exception("subtitle save raised")
        return []


def _resolve_saved_paths(video, saved, directory: Path | None) -> list[Path]:
    """Map subliminal's saved-subtitle objects to on-disk paths that exist."""
    paths: list[Path] = []
    for sub in saved:
        try:
            written = sub.get_path(video)
        except Exception:
            log.exception("subtitle.get_path failed for %s", sub)
            continue
        target = directory / Path(written).name if directory is not None else Path(written)
        if target.exists():
            paths.append(target)
        else:
            log.warning("expected subtitle at %s but it's missing", target)
    return paths
