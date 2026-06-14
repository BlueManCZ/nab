import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from nab import MEDIA_EXTENSIONS

log = logging.getLogger(__name__)


_MAGNET_FRAGMENT = "#file="


def split_magnet_fragment(uri: str) -> tuple[str, str | None]:
    """Split a nab-specific ``#file=<sub_path>`` fragment off a magnet URI.

    Returns ``(base_uri, sub_path)``. ``sub_path`` is None when no fragment
    is present. The fragment is the path of a file inside the torrent — we
    use it to key history per-episode and to drive file selection when the
    user replays a specific episode.
    """
    if _MAGNET_FRAGMENT not in uri:
        return uri, None
    base, _, frag = uri.partition(_MAGNET_FRAGMENT)
    return base, unquote(frag)


def join_magnet_fragment(base_uri: str, sub_path: str) -> str:
    """Build a magnet URI that names a specific file inside the torrent."""
    return f"{base_uri}{_MAGNET_FRAGMENT}{quote(sub_path, safe='')}"


def magnet_for_cache_path(
    missing_path: str, cache_dir: Path, magnet_inputs: Iterable[str]
) -> str | None:
    """Find the magnet that can re-fetch a deleted torrent-cache file.

    A finished torrent can be played straight from the cache as a plain local
    file; clearing the cache later strands that history entry. The cached file
    lives at ``<cache_dir>/<sub_path>`` and the magnet that produced it carries
    the same ``<sub_path>`` in its ``#file=`` fragment, so matching on that maps
    the orphaned path back to a magnet we can re-download. Returns that magnet
    input, or None when the path isn't a (recoverable) cache file.
    """
    try:
        sub_path = str(
            Path(missing_path).expanduser().resolve().relative_to(cache_dir.resolve())
        )
    except (ValueError, OSError):
        return None  # not under the torrent cache dir
    for magnet in magnet_inputs:
        _, frag = split_magnet_fragment(magnet)
        if frag == sub_path:
            return magnet
    return None


class SourceType(str, Enum):
    LOCAL_FILE = "local_file"
    MAGNET = "magnet"
    WEB_URL = "web_url"
    DIRECT_URL = "direct_url"


@dataclass(slots=True)
class ResolvedSource:
    source_type: SourceType
    playable_url: str
    title: str | None
    duration: float | None
    original_input: str


def _classify(input_str: str) -> SourceType:
    if input_str.startswith("magnet:?"):
        # Our own ``#file=`` fragment lives on a magnet URI too.
        return SourceType.MAGNET
    if input_str.startswith("file://"):
        return SourceType.LOCAL_FILE
    if input_str.startswith(("/", "~/", "./")):
        return SourceType.LOCAL_FILE
    parsed = urlparse(input_str)
    if parsed.scheme in ("http", "https"):
        path_lower = parsed.path.lower()
        if Path(path_lower).suffix in MEDIA_EXTENSIONS:
            return SourceType.DIRECT_URL
        return SourceType.WEB_URL
    return SourceType.LOCAL_FILE


def _resolve_local(input_str: str) -> ResolvedSource:
    if input_str.startswith("file://"):
        path = Path(unquote(urlparse(input_str).path))
    else:
        path = Path(input_str).expanduser()
    path = path.resolve()
    if not path.exists():
        # Carry the normalised path on the exception so a cache miss can be
        # mapped back to the magnet that re-fetches it (see magnet_for_cache_path).
        err = FileNotFoundError(f"No such file: {path}")
        err.filename = str(path)
        raise err
    return ResolvedSource(
        source_type=SourceType.LOCAL_FILE,
        playable_url=str(path),
        title=path.name,
        duration=None,
        original_input=input_str,
    )


def _direct_url(input_str: str) -> ResolvedSource:
    return ResolvedSource(
        source_type=SourceType.DIRECT_URL,
        playable_url=input_str,
        title=None,
        duration=None,
        original_input=input_str,
    )


def _resolve_magnet(
    input_str: str, on_progress: Callable[[str], None] | None
) -> ResolvedSource:
    from nab.torrent import is_available

    if not is_available():
        raise NotImplementedError(
            "Magnet links require libtorrent, which is not installed. "
            "Install it via your system package manager."
        )
    from nab.torrent.engine import get_engine

    base_uri, requested_sub_path = split_magnet_fragment(input_str)
    engine = get_engine()
    info = engine.open_magnet(base_uri, on_progress=on_progress)

    idx: int | None = None
    if requested_sub_path is not None:
        match = info.by_sub_path(requested_sub_path)
        if match is not None:
            idx = match.index
        else:
            log.warning(
                "requested file %r not in torrent; falling back to default",
                requested_sub_path,
            )
    if idx is None:
        idx = engine.default_file_index(info)

    url, title = engine.select_file(idx, on_progress=on_progress)

    # Normalise the input to always carry the file fragment. The history
    # row is keyed on this string, so per-episode resume positions work
    # whether the user pasted a bare magnet or clicked a history entry.
    selected = info.by_index(idx)
    assert selected is not None  # idx came from this same info
    normalised_input = join_magnet_fragment(base_uri, selected.sub_path)

    return ResolvedSource(
        source_type=SourceType.MAGNET,
        playable_url=url,
        title=title,
        duration=None,
        original_input=normalised_input,
    )


def _resolve_web(input_str: str) -> ResolvedSource:
    from nab.resolver.ytdlp import extract

    try:
        return extract(input_str)
    except Exception as exc:
        log.warning("yt-dlp failed for %r: %s; falling back to direct URL", input_str, exc)
        return _direct_url(input_str)


def resolve(
    input_str: str, on_progress: Callable[[str], None] | None = None
) -> ResolvedSource:
    """Classify input and produce a playable URL plus metadata.

    Runs network calls (yt-dlp). Call this from a worker thread.
    """
    input_str = input_str.strip()
    kind = _classify(input_str)
    log.info("classified %r as %s", input_str, kind.value)

    if kind is SourceType.LOCAL_FILE:
        return _resolve_local(input_str)
    if kind is SourceType.MAGNET:
        return _resolve_magnet(input_str, on_progress)
    if kind is SourceType.DIRECT_URL:
        return _direct_url(input_str)
    return _resolve_web(input_str)
