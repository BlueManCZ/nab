import logging
import threading
from collections.abc import Callable
from functools import cache, wraps
from pathlib import Path
from typing import TYPE_CHECKING

from nab import SUBTITLE_EXTENSIONS
from nab.paths import cache_home
from nab.resolver import ResolvedSource, SourceType
from nab.resolver.source import TORRENT_SOURCE_TYPES, split_torrent_fragment
from nab.subtitles.config import SubtitlesConfig

if TYPE_CHECKING:
    from subliminal import Video

log = logging.getLogger(__name__)

# What a planner hands back: the video to search on plus where to save
# (None meaning "next to the video"), a ready-made list of subtitles that
# make searching unnecessary, or None to skip this source entirely.
type SearchPlan = tuple["Video", Path | None] | list[Path] | None

_FREE_PROVIDERS = ["podnapisi", "gestdown", "tvsubtitles"]

# Source types we can look subtitles up for. Local files are scanned off
# disk; torrents are matched on their release name. A web/direct URL gives us
# neither a file to hash nor a reliable release name, so it's out.
DISCOVERABLE_SOURCE_TYPES = (
    frozenset({SourceType.LOCAL_FILE}) | TORRENT_SOURCE_TYPES
)


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


def sidecar_subtitles(directory: Path, stem: str) -> list[Path]:
    """Subtitle files in `directory` named for a video with `stem`.

    Matches both the bare `<stem>.srt` and the language-tagged
    `<stem>.en.srt` that subliminal writes.
    """
    prefix = stem.lower()
    found: list[Path] = []
    try:
        for sibling in directory.iterdir():
            if not sibling.is_file():
                continue
            if sibling.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            sub_stem = sibling.stem.lower()
            if sub_stem == prefix or sub_stem.startswith(f"{prefix}."):
                found.append(sibling)
    except OSError:
        return []
    return sorted(found)


def has_existing_subtitles(video_path: Path) -> bool:
    """True if a sidecar subtitle file already exists for `video_path`."""
    return bool(sidecar_subtitles(video_path.parent, video_path.stem))


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


def _subs_cache_dir(key: str | None = None) -> Path:
    """The directory cached subtitles live in, optionally scoped by `key`.

    Torrents pass their info-hash: the file inside a torrent is often named
    something generic (`movie.mkv`), and a flat cache would hand one film's
    subtitles to the next one with the same inner filename.
    """
    d = cache_home() / "subtitles"
    if key:
        d /= key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _release_name(source: ResolvedSource) -> str | None:
    """The torrent's in-torrent path, which is what guessit reads best.

    Prefers the full `#file=` sub-path over the bare filename: the parent
    directory usually carries the release name (`Movie.2023.1080p-GRP/`),
    and guessit uses that context when the filename alone is thin.
    """
    _, sub_path = split_torrent_fragment(source.original_input)
    return sub_path or source.title


def _torrent_cache_key(source: ResolvedSource) -> str | None:
    """The open torrent's info-hash, used to scope its cached subtitles.

    Read straight off the source rather than asked of the engine, so it stays
    pinned to the torrent this search is for even if the user has since
    started another one.
    """
    from nab.torrent import is_available

    if not is_available():
        return None
    from nab.torrent.engine import torrent_identity

    base_uri, _ = split_torrent_fragment(source.original_input)
    return torrent_identity(base_uri)


def _plan_local(
    source: ResolvedSource, config: SubtitlesConfig, force: bool
) -> SearchPlan:
    """Build the search plan for a local file."""
    from subliminal import scan_video

    video_path = Path(source.playable_url)
    if not video_path.is_file():
        return None

    if not force and has_existing_subtitles(video_path):
        log.info("subtitles already present for %s; skipping auto-download", video_path.name)
        return []

    try:
        video = scan_video(str(video_path))
    except Exception:
        log.exception("scan_video failed for %s", video_path)
        return None

    # Next to the video by default so mpv auto-loads the sidecar on replay.
    return video, (None if config.save_next_to_video else _subs_cache_dir())


def _plan_torrent(source: ResolvedSource, force: bool) -> SearchPlan:
    """Build the search plan for a magnet or .torrent file.

    Matches on the release name rather than scanning the file: the torrent
    cache holds a partially-downloaded sparse image whose hash is meaningless,
    and the providers we query are name-based anyway. That also means this can
    run before a single byte has landed.

    Subtitles always go to the cache dir — mpv can't auto-load a sidecar next
    to a file it's streaming over HTTP, and writing into the torrent cache
    would lose them the next time the user clears it.
    """
    from subliminal import Video

    release = _release_name(source)
    if not release:
        return None

    save_dir = _subs_cache_dir(_torrent_cache_key(source))
    if not force:
        cached = sidecar_subtitles(save_dir, Path(release).stem)
        if cached:
            log.info("reusing %d cached subtitle(s) for %s", len(cached), release)
            return cached

    try:
        video = Video.fromname(release)
    except Exception:
        log.info("couldn't parse a title out of %r; skipping subtitles", release)
        return None

    return video, save_dir


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
    if source.source_type not in DISCOVERABLE_SOURCE_TYPES:
        return []

    try:
        from subliminal import download_best_subtitles
    except ImportError:
        _warn_missing()
        return []

    plan = (
        _plan_local(source, config, force)
        if source.source_type is SourceType.LOCAL_FILE
        else _plan_torrent(source, force)
    )
    if plan is None:
        return []
    if isinstance(plan, list):
        return plan  # already satisfied, no search needed
    video, save_dir = plan

    _configure_cache()

    if cancel_token is not None and cancel_token.is_set():
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
        log.exception("subliminal download failed for %s", video.name)
        return []

    found = result.get(video, [])
    if not found:
        log.info("no subtitles found for %s", video.name)
        return []

    if cancel_token is not None and cancel_token.is_set():
        return []

    # `save_dir is None` means "next to the video"; if that directory isn't
    # writable, fall back to the cache dir.
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
