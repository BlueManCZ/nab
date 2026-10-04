import errno
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from nab import MEDIA_EXTENSIONS

log = logging.getLogger(__name__)


# Both torrent source forms — a magnet URI and a path to a .torrent file —
# carry this fragment to name one file inside the torrent.
_FILE_FRAGMENT = "#file="
_MAGNET_PREFIX = "magnet:?"
_INFO_HASH_RE = re.compile(r"xt=urn:btih:([a-zA-Z0-9]+)", re.IGNORECASE)
_HTTP_SCHEMES = ("http", "https")

TORRENT_FILE_SUFFIX = ".torrent"


def is_magnet_uri(uri: str) -> bool:
    """True when `uri` is a magnet link rather than a .torrent file or URL."""
    return uri.startswith(_MAGNET_PREFIX)


def is_http_url(uri: str) -> bool:
    """True when `uri` is an http(s) URL rather than a magnet or a local path."""
    return urlparse(uri).scheme in _HTTP_SCHEMES


def magnet_info_hash(magnet_uri: str) -> str | None:
    """The info-hash a magnet URI points at, lowercased, or None if malformed.

    This is the torrent's stable identity, so it also serves as a per-torrent
    key for anything cached on disk. A .torrent file's identity has to be read
    out of the file itself — see ``nab.torrent.engine.torrent_identity``.
    """
    m = _INFO_HASH_RE.search(magnet_uri)
    return m.group(1).lower() if m else None


def local_path(input_str: str) -> Path:
    """The absolute filesystem path `input_str` names.

    Accepts both a `file://` URI and a plain (possibly `~`-relative) path, so
    callers don't care which form the file manager, CLI or history handed them.
    """
    raw = unquote(urlparse(input_str).path) if input_str.startswith("file://") else input_str
    return Path(raw).expanduser().resolve()


def split_torrent_fragment(uri: str) -> tuple[str, str | None]:
    """Split a nab-specific ``#file=<sub_path>`` fragment off a torrent source.

    Returns ``(base_uri, sub_path)``. ``sub_path`` is None when no fragment
    is present. The fragment is the path of a file inside the torrent — we
    use it to key history per-episode and to drive file selection when the
    user replays a specific episode.
    """
    if _FILE_FRAGMENT not in uri:
        return uri, None
    base, _, frag = uri.partition(_FILE_FRAGMENT)
    return base, unquote(frag)


def join_torrent_fragment(base_uri: str, sub_path: str) -> str:
    """Build a torrent source that names a specific file inside the torrent."""
    return f"{base_uri}{_FILE_FRAGMENT}{quote(sub_path, safe='')}"


def torrent_for_cache_path(
    missing_path: str, cache_dir: Path, torrent_inputs: Iterable[str]
) -> str | None:
    """Find the torrent source that can re-fetch a deleted torrent-cache file.

    A finished torrent can be played straight from the cache as a plain local
    file; clearing the cache later strands that history entry. The cached file
    lives at ``<cache_dir>/<sub_path>`` and the magnet (or .torrent) that
    produced it carries the same ``<sub_path>`` in its ``#file=`` fragment, so
    matching on that maps the orphaned path back to a source we can
    re-download. Returns that input, or None when the path isn't a
    (recoverable) cache file.
    """
    try:
        sub_path = str(
            Path(missing_path).expanduser().resolve().relative_to(cache_dir.resolve())
        )
    except (ValueError, OSError):
        return None  # not under the torrent cache dir
    for candidate in torrent_inputs:
        _, frag = split_torrent_fragment(candidate)
        if frag == sub_path:
            return candidate
    return None


class SourceType(str, Enum):
    LOCAL_FILE = "local_file"
    MAGNET = "magnet"
    TORRENT_FILE = "torrent_file"
    TORRENT_URL = "torrent_url"
    WEB_URL = "web_url"
    DIRECT_URL = "direct_url"

    @property
    def is_torrent(self) -> bool:
        """True for the sources streamed through the torrent engine."""
        return self in TORRENT_SOURCE_TYPES


# The torrent sources differ only in how the torrent is obtained; everything
# downstream (file picking, bundled subtitles, the live stats card) treats them
# alike, so it keys off this set rather than naming each one.
TORRENT_SOURCE_TYPES = frozenset({
    SourceType.MAGNET, SourceType.TORRENT_FILE, SourceType.TORRENT_URL,
})

# How to name each torrent source in a user-facing message.
_TORRENT_LABELS = {
    SourceType.MAGNET: "Magnet links",
    SourceType.TORRENT_FILE: "Torrent files",
    SourceType.TORRENT_URL: "Torrent links",
}


@dataclass(slots=True)
class ResolvedSource:
    source_type: SourceType
    playable_url: str
    title: str | None
    duration: float | None
    original_input: str


def _classify(input_str: str) -> SourceType:
    # Only torrent sources carry our ``#file=`` fragment, so strip it before
    # looking at the input — a magnet or .torrent path is still one with it.
    base, _ = split_torrent_fragment(input_str)
    if is_magnet_uri(base):
        return SourceType.MAGNET
    parsed = urlparse(base)
    if parsed.scheme in _HTTP_SCHEMES:
        # Match on the path alone — a tracker's download link often carries a
        # query string (`/x.torrent?token=…`) that we don't want to read.
        suffix = Path(parsed.path.lower()).suffix
        if suffix == TORRENT_FILE_SUFFIX:
            return SourceType.TORRENT_URL
        if suffix in MEDIA_EXTENSIONS:
            return SourceType.DIRECT_URL
        return SourceType.WEB_URL
    # Anything else is a path on disk: a .torrent goes through the engine,
    # everything else straight to mpv.
    if local_path(base).suffix.lower() == TORRENT_FILE_SUFFIX:
        return SourceType.TORRENT_FILE
    return SourceType.LOCAL_FILE


def _missing_file(path: Path) -> FileNotFoundError:
    """A "no such file" error carrying the normalised path.

    The filename rides on the exception so a cache miss can be mapped back to
    the torrent that re-fetches it (see ``torrent_for_cache_path``). It has to
    go through OSError's (errno, strerror, filename) form: setting ``filename``
    on an error built from a plain message makes ``str()`` drop that message
    and render "[Errno None] None: …" instead.
    """
    return FileNotFoundError(errno.ENOENT, "No such file", str(path))


def _resolve_local(input_str: str) -> ResolvedSource:
    path = local_path(input_str)
    if not path.exists():
        raise _missing_file(path)
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


def _prepare_torrent_source(
    base: str, kind: SourceType, on_progress: Callable[[str], None] | None
) -> str:
    """Make `base` something the engine can open, and return its final form.

    A magnet passes through untouched. A local .torrent is normalised to an
    absolute path, so history keys one row per torrent however the file was
    handed to us (file dialog, drag-and-drop, CLI argument, `file://` URI). A
    remote one is downloaded into the metainfo cache but keeps its URL: the
    engine reads the cached copy (see ``nab.torrent.engine.torrent_identity``),
    while history replays the link rather than a cache path the user may since
    have cleared.
    """
    if kind is SourceType.MAGNET:
        return base
    if kind is SourceType.TORRENT_URL:
        from nab.resolver.metainfo import fetch

        fetch(base, on_progress=on_progress)
        return base
    path = local_path(base)
    if not path.is_file():
        raise _missing_file(path)
    return str(path)


def _resolve_torrent(
    input_str: str, kind: SourceType, on_progress: Callable[[str], None] | None
) -> ResolvedSource:
    """Open a torrent source and stream one video file out of it."""
    from nab.torrent import is_available

    if not is_available():
        raise NotImplementedError(
            f"{_TORRENT_LABELS[kind]} require libtorrent, which is not installed. "
            "Install it via your system package manager."
        )
    from nab.torrent.engine import get_engine

    base_uri, requested_sub_path = split_torrent_fragment(input_str)
    base_uri = _prepare_torrent_source(base_uri, kind, on_progress)
    engine = get_engine()
    info = engine.open_torrent(base_uri, on_progress=on_progress)

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
    normalised_input = join_torrent_fragment(base_uri, selected.sub_path)

    return ResolvedSource(
        source_type=kind,
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

    if kind.is_torrent:
        return _resolve_torrent(input_str, kind, on_progress)
    if kind is SourceType.LOCAL_FILE:
        return _resolve_local(input_str)
    if kind is SourceType.DIRECT_URL:
        return _direct_url(input_str)
    return _resolve_web(input_str)
