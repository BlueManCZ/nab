"""Torrent streaming engine backed by libtorrent."""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from nab import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS
from nab.resolver.source import is_http_url, is_magnet_uri, magnet_info_hash
from nab.series import detect_series
from nab.torrent.config import TorrentConfig
from nab.torrent.http_server import ByteRangeServer

log = logging.getLogger(__name__)

_INITIAL_PIECES_TIMEOUT = 60
_PIECE_WAIT_TIMEOUT = 30
_RESUME_SAVE_TIMEOUT = 5

# libtorrent file priorities. The streaming file dominates; prefetched
# episodes trickle in behind it on a low (but non-zero) priority so they
# don't compete with playback for bandwidth. 0 means "don't download".
_STREAM_PRIORITY = 4
_PREFETCH_PRIORITY = 1
# Bundled sidecars are a few hundred KB, so they ride at the top priority
# without meaningfully competing with the stream — and a release's own subs
# are usually better synced than anything we can scrape.
_SUBTITLE_PRIORITY = 7

# Sidecar formats we're willing to pull alongside the stream. Image-based
# tracks (VobSub .sub/.idx, PGS .sup) run to tens of megabytes, so they're
# excluded by extension; the size cap catches anything else oversized.
_BUNDLED_SUBTITLE_EXTENSIONS = SUBTITLE_EXTENSIONS - {".sub", ".idx", ".sup"}
_MAX_BUNDLED_SUBTITLE_SIZE = 4 << 20
# Milliseconds, relative to now — set after the initial video pieces so
# buffering the stream still wins the race.
_SUBTITLE_PIECE_DEADLINE_MS = 2000

_EPISODE_TOKEN_RE = re.compile(r"s\d{1,2}e\d{1,3}", re.IGNORECASE)

ProgressCallback = Callable[[str], None]


def _noop_progress(_msg: str) -> None:
    pass


def _format_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 10):.0f} KB"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _metadata_progress(s: lt.torrent_status, elapsed: float) -> str:
    """Build the status line shown while waiting for a magnet's metadata.

    The lead phrase tracks the actual phase, and distinguishes *discovered*
    peers (found via DHT/trackers) from *connected* ones: that gap is the
    difference between "the swarm is dead" and "we found it but can't connect",
    which a bare connected-count hides.
    """
    connected, discovered = s.num_peers, s.list_peers
    secs = f"{int(elapsed)}s"
    if connected == 0:
        if discovered:
            return f"Found {_plural(discovered, 'peer')}, connecting… · {secs}"
        return f"Searching for peers… · {secs}"
    peers = (
        f"{connected} of {discovered} peers"
        if discovered > connected
        else _plural(connected, "peer")
    )
    if s.num_seeds:
        peers += f", {_plural(s.num_seeds, 'seed')}"
    return f"Fetching metadata… {peers} · {secs}"


def _cache_dir() -> Path:
    d = TorrentConfig.load().effective_cache_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resume_path(info_hash: str) -> Path:
    return _cache_dir() / f"{info_hash}.resume"


def _metainfo_path(uri: str) -> str:
    """The .torrent file `uri` names: itself, or its cached download.

    Lets every entry point below take any torrent source uniformly — a URL
    resolves to the copy ``nab.resolver.metainfo`` pulled down for it, which
    the resolver has already made sure is there.
    """
    if not is_http_url(uri):
        return uri
    from nab.resolver.metainfo import cache_path

    return str(cache_path(uri))


def _read_torrent_file(uri: str) -> lt.torrent_info:
    """Parse the .torrent file `uri` names into libtorrent's metadata object.

    Raises ValueError (rather than libtorrent's RuntimeError) so a bad file
    surfaces to the user the same way any other unusable input does — naming
    the source they gave us, not the cache path we read it from.
    """
    try:
        return lt.torrent_info(_metainfo_path(uri))
    except Exception as exc:
        raise ValueError(f"Not a readable .torrent file: {uri}") from exc


def _hash_string(hashes: lt.info_hash_t) -> str:
    """Hex identity for a torrent, preferring its v1 info-hash.

    Magnets carry the v1 hash in ``urn:btih:``, so preferring v1 means a
    hybrid torrent opened as a file and as a magnet share one identity — and
    with it one resume file and one subtitle cache directory.
    """
    return str(hashes.v1 if hashes.has_v1() else hashes.v2)


def torrent_identity(uri: str) -> str | None:
    """The info-hash the torrent source `uri` names, read without the swarm.

    Every form carries its identity locally: a magnet in its ``urn:btih:``
    parameter, a .torrent file — downloaded or on disk — in its info-dict.
    Returns None when none of them yields one (a magnet with no info-hash, an
    unreadable or not-yet-fetched file).
    """
    if is_magnet_uri(uri):
        return magnet_info_hash(uri)
    try:
        return _hash_string(_read_torrent_file(uri).info_hashes())
    except Exception as exc:
        log.warning("couldn't read an info-hash out of %s: %s", uri, exc)
        return None


@dataclass(slots=True, frozen=True)
class TorrentFile:
    """One file inside a torrent, as exposed to the rest of the app."""
    index: int
    sub_path: str    # path inside the torrent, e.g. "Show.S01/E01.mkv"
    size: int

    @property
    def name(self) -> str:
        return Path(self.sub_path).name


@dataclass(slots=True, frozen=True)
class TorrentStatus:
    """Live transfer stats for the open torrent, polled while streaming."""
    download_rate: int  # bytes/s
    upload_rate: int    # bytes/s
    num_peers: int      # connected peers
    num_seeds: int      # connected seeds
    total_done: int     # selected-file bytes downloaded so far
    total_wanted: int   # selected-file size in bytes


@dataclass(slots=True, frozen=True)
class TorrentInfo:
    """Metadata about an open torrent. video_files is sorted by sub_path.

    ``subtitle_files`` holds the sidecars worth fetching (see
    ``_BUNDLED_SUBTITLE_EXTENSIONS``), not every subtitle in the torrent.
    """
    info_hash: str | None
    video_files: tuple[TorrentFile, ...]
    subtitle_files: tuple[TorrentFile, ...] = ()

    def by_index(self, index: int) -> TorrentFile | None:
        return next((f for f in self.video_files if f.index == index), None)

    def by_sub_path(self, sub_path: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.sub_path == sub_path), None)

    def by_name(self, name: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.name == name), None)


def match_subtitle_files(
    video: TorrentFile,
    subtitles: tuple[TorrentFile, ...],
    *,
    sole_video: bool,
) -> list[TorrentFile]:
    """Pick the sidecars in `subtitles` that belong to `video`.

    Releases label their subs in one of three ways, tried in order of how
    specific they are:

    1. Sidecar naming — ``Movie.mkv`` next to ``Movie.en.srt``.
    2. A shared ``Subs/`` directory keyed by episode, where only the SxxEyy
       token ties a sub back to its episode (``Subs/S01E03/2_English.srt``).
    3. A single-video torrent, where whatever subs it ships are necessarily
       for that video.

    Returns [] when none of those apply — better no subtitles than the wrong
    episode's.
    """
    if not subtitles:
        return []

    stem = Path(video.sub_path).stem.lower()
    same_stem = [
        s
        for s in subtitles
        if (sub_stem := Path(s.sub_path).stem.lower()) == stem
        or sub_stem.startswith(f"{stem}.")
    ]
    if same_stem:
        return same_stem

    token = _EPISODE_TOKEN_RE.search(video.name)
    if token is not None:
        needle = token.group(0).lower()
        tagged = [s for s in subtitles if needle in s.sub_path.lower()]
        if tagged:
            return tagged

    return list(subtitles) if sole_video else []


class TorrentEngine:
    """Manages a single libtorrent session for streaming torrents.

    Lifecycle: ``open_torrent`` adds a torrent — named by a magnet link or a
    .torrent file — and returns its file list; ``select_file`` switches the
    active file inside the open torrent and starts (or restarts) the HTTP
    server. Re-opening the torrent that's already open returns the cached file
    list without re-adding it.
    """

    def __init__(self):
        self._session: lt.session | None = None
        self._handle: lt.torrent_handle | None = None
        self._info: TorrentInfo | None = None
        self._info_hash: str | None = None
        self._server: ByteRangeServer | None = None
        self._file_index: int = -1
        self._piece_length: int = 0
        self._file_offset: int = 0
        self._file_size: int = 0
        self._file_path: str = ""
        self._subtitle_files: tuple[TorrentFile, ...] = ()

    def _ensure_session(self) -> lt.session:
        if self._session is not None:
            return self._session
        self._session = lt.session({
            "enable_dht": True,
            "enable_lsd": True,
            "enable_natpmp": True,
            "enable_upnp": True,
            "user_agent": "nab/0.1",
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
        })
        return self._session

    def _add_torrent_params(
        self, uri: str, info_hash: str | None
    ) -> lt.add_torrent_params:
        """Build the params to hand ``add_torrent``.

        Prefers resume data saved on a previous run: it carries the info-dict
        (the torrent metadata), so re-opening skips the swarm metadata fetch
        entirely and rechecks the already-cached files locally. Falls back to
        the source itself — a bare magnet parse, or the .torrent file's own
        info-dict — if there's no resume file or it's unreadable (wrong
        version, truncated write, …). `uri` is any torrent source: a magnet, a
        path, or the URL a metainfo file was fetched from.
        """
        if info_hash is not None:
            path = _resume_path(info_hash)
            if path.is_file():
                try:
                    params = lt.read_resume_data(path.read_bytes())
                    params.save_path = str(_cache_dir())
                    log.info("loaded resume data for %s", info_hash)
                    return params
                except Exception:
                    log.exception("unreadable resume data %s; refetching", path)
        if is_magnet_uri(uri):
            params = lt.parse_magnet_uri(uri)
        else:
            params = lt.add_torrent_params()
            params.ti = _read_torrent_file(uri)
        params.save_path = str(_cache_dir())
        return params

    def open_torrent(
        self,
        uri: str,
        on_progress: ProgressCallback | None = None,
    ) -> TorrentInfo:
        """Add the torrent to the session (if new) and return its video file list.

        `uri` is a magnet link, a path to a .torrent file, or the URL one was
        fetched from. Blocks until metadata arrives — immediately for a
        .torrent file, which ships its own info-dict; from the swarm for a
        bare magnet. Re-opening the torrent that's already open returns the
        in-memory file list for free. Across runs (or after switching
        torrents), resume data saved on ``cleanup`` carries the metadata back,
        so the metadata fetch is skipped rather than re-run against a
        possibly-dead swarm.
        """
        progress = on_progress or _noop_progress

        new_hash = torrent_identity(uri)
        if (
            self._handle is not None
            and self._info is not None
            and new_hash is not None
            and self._info_hash == new_hash
        ):
            log.info("reusing open torrent %s", new_hash)
            return self._info

        # Build the params first: an unusable source (corrupt .torrent file,
        # unparseable magnet) raises here, before we've torn down whatever is
        # currently streaming.
        params = self._add_torrent_params(uri, new_hash)

        # Different torrent (or never opened) — drop the old one.
        self.cleanup()
        ses = self._ensure_session()
        self._handle = ses.add_torrent(params)
        self._info_hash = new_hash
        h = self._handle

        # A .torrent file (or recovered resume data) hands us the metadata up
        # front, so skip straight past the swarm wait and its progress chatter.
        if not h.status().has_metadata:
            timeout = TorrentConfig.load().effective_metadata_timeout
            progress("Connecting to swarm…")
            log.info("waiting for torrent metadata...")
            start = time.monotonic()
            deadline = start + timeout
            while not h.status().has_metadata:
                now = time.monotonic()
                if now > deadline:
                    self.cleanup()
                    raise TimeoutError(
                        f"Timed out waiting for torrent metadata ({timeout}s). "
                        "The swarm may be dead or unreachable."
                    )
                progress(_metadata_progress(h.status(), now - start))
                time.sleep(0.5)
            log.info("metadata received")

        ti = h.torrent_file()
        fs = ti.files()
        videos: list[TorrentFile] = []
        subtitles: list[TorrentFile] = []
        for i in range(fs.num_files()):
            sub_path = fs.file_path(i)
            size = fs.file_size(i)
            suffix = Path(sub_path).suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                videos.append(TorrentFile(index=i, sub_path=sub_path, size=size))
            elif (
                suffix in _BUNDLED_SUBTITLE_EXTENSIONS
                and size <= _MAX_BUNDLED_SUBTITLE_SIZE
            ):
                subtitles.append(TorrentFile(index=i, sub_path=sub_path, size=size))

        if not videos:
            names = [fs.file_path(i) for i in range(fs.num_files())]
            self.cleanup()
            raise ValueError(
                f"No video file found in torrent. Files: {', '.join(names)}"
            )

        videos.sort(key=lambda f: f.sub_path)
        subtitles.sort(key=lambda f: f.sub_path)
        self._info = TorrentInfo(
            info_hash=new_hash,
            video_files=tuple(videos),
            subtitle_files=tuple(subtitles),
        )
        log.info(
            "torrent has %d video file(s), %d bundled subtitle(s)",
            len(videos), len(subtitles),
        )
        return self._info

    def select_file(
        self,
        file_index: int,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[str, str]:
        """Switch the active file in the open torrent. Returns ``(http_url, title)``.

        Reprioritises pieces, restarts the HTTP server on the new file, and
        blocks until enough initial data is buffered for playback. Calling
        with the currently-selected ``file_index`` is a no-op that returns
        the existing URL.
        """
        if self._handle is None or self._info is None:
            raise RuntimeError("No torrent open; call open_torrent first.")

        progress = on_progress or _noop_progress

        if self._file_index == file_index and self._server is not None:
            return self._server.url, Path(self._file_path).name

        h = self._handle
        ti = h.torrent_file()
        fs = ti.files()
        if file_index < 0 or file_index >= fs.num_files():
            raise IndexError(f"File index {file_index} out of range")

        # Stop the old server before reprioritising. A late HTTP range
        # request reading from the old file after we've zeroed its pieces
        # would block forever in wait_for_piece.
        if self._server is not None:
            self._server.stop()
            self._server = None

        self._file_index = file_index
        self._piece_length = ti.piece_length()
        self._file_offset = fs.file_offset(file_index)
        self._file_size = fs.file_size(file_index)
        self._file_path = str(_cache_dir() / fs.file_path(file_index))
        title = fs.file_name(file_index)

        log.info(
            "selected file %d: %s (%d bytes, piece_length=%d)",
            file_index, title, self._file_size, self._piece_length,
        )
        progress(f"Selecting: {title} ({_format_size(self._file_size)})")

        priorities = [0] * fs.num_files()
        priorities[file_index] = _STREAM_PRIORITY
        ahead = TorrentConfig.load().effective_download_ahead
        prefetch = self._prefetch_indices(file_index, ahead)
        for idx in prefetch:
            priorities[idx] = _PREFETCH_PRIORITY
        if prefetch:
            log.info("prefetching %d upcoming file(s): %s", len(prefetch), prefetch)

        self._subtitle_files = tuple(self._bundled_subtitles_for(file_index))
        for sub in self._subtitle_files:
            priorities[sub.index] = _SUBTITLE_PRIORITY
        if self._subtitle_files:
            log.info(
                "fetching %d bundled subtitle(s): %s",
                len(self._subtitle_files),
                [s.sub_path for s in self._subtitle_files],
            )

        h.prioritize_files(priorities)
        h.set_sequential_download(True)

        first_piece = self._file_offset // self._piece_length
        last_piece = (self._file_offset + self._file_size - 1) // self._piece_length

        for i in range(min(32, ti.num_pieces())):
            p = first_piece + i
            if p < ti.num_pieces():
                h.set_piece_deadline(p, i * 50)
        for i in range(min(4, ti.num_pieces())):
            p = last_piece - i
            if 0 <= p < ti.num_pieces():
                h.set_piece_deadline(p, 500 + i * 50)

        # Sequential download would leave a sidecar stored after the video
        # until the whole video is in. Deadline them just behind the play
        # buffer instead, so they land seconds into playback.
        for sub in self._subtitle_files:
            sub_start = fs.file_offset(sub.index)
            sub_end = sub_start + max(sub.size, 1) - 1
            for p in range(
                sub_start // self._piece_length, sub_end // self._piece_length + 1
            ):
                if 0 <= p < ti.num_pieces():
                    h.set_piece_deadline(p, _SUBTITLE_PIECE_DEADLINE_MS)

        log.info("waiting for first piece...")
        deadline = time.monotonic() + _INITIAL_PIECES_TIMEOUT
        while not h.have_piece(first_piece):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for initial torrent data ({_INITIAL_PIECES_TIMEOUT}s)"
                )
            s = h.status()
            rate = s.download_rate / 1024
            progress(f"Buffering {title}… ({rate:.0f} KB/s)")
            time.sleep(0.3)

        log.info("first piece ready, starting HTTP server")
        progress("Starting stream…")
        self._server = ByteRangeServer(self._file_path, self._file_size, self)
        url = self._server.start()
        return url, title

    def _prefetch_indices(self, current_index: int, count: int) -> list[int]:
        """File indices of the next ``count`` videos to download ahead.

        Ordered the way the series navigator orders episodes — by the detected
        numeric axis (S01E01, S01E02, …) when there is one, else by the
        torrent's sub-path order — so "download ahead" lines up with "what
        plays next". Returns fewer than ``count`` near the end of the series,
        and an empty list when prefetch is off or the open torrent doesn't
        recognise the current file.
        """
        info = self._info
        if info is None or count <= 0:
            return []
        files = info.video_files
        current = info.by_index(current_index)
        if current is None:
            return []

        names = [f.name for f in files]
        view = detect_series(current.name, [n for n in names if n != current.name])
        if view is not None:
            upcoming = []
            for item in view.items[view.current_index + 1 : view.current_index + 1 + count]:
                f = info.by_name(item.name)
                if f is not None and f.index != current_index:
                    upcoming.append(f.index)
            return upcoming

        pos = files.index(current)
        return [f.index for f in files[pos + 1 : pos + 1 + count]]

    def default_file_index(self, info: TorrentInfo) -> int:
        """Pick a sensible file to play when the user didn't name a file.

        If the videos look like a series (S01E01, S01E02, …), pick the
        lowest-numbered episode. Otherwise pick the largest video (which is
        the right call for "movie + bonus features" torrents).
        """
        if not info.video_files:
            raise ValueError("No video files")
        if len(info.video_files) == 1:
            return info.video_files[0].index

        # Try every video as the detection anchor and keep the biggest series
        # found. Anchoring on a fixed file (e.g. the alphabetically first) is
        # fragile: a sample or bonus track that sorts before the episodes
        # misses the pattern and we end up falling through to "largest."
        names = [f.name for f in info.video_files]
        best_view = None
        for i, candidate in enumerate(names):
            siblings = names[:i] + names[i + 1:]
            view = detect_series(candidate, siblings)
            if view is None:
                continue
            if best_view is None or len(view.items) > len(best_view.items):
                best_view = view

        if best_view is not None:
            match = info.by_name(best_view.items[0].name)
            if match is not None:
                return match.index

        return max(info.video_files, key=lambda f: f.size).index

    def _bundled_subtitles_for(self, file_index: int) -> list[TorrentFile]:
        """Sidecars in the torrent belonging to the file at `file_index`."""
        info = self._info
        if info is None:
            return []
        video = info.by_index(file_index)
        if video is None:
            return []
        return match_subtitle_files(
            video,
            info.subtitle_files,
            sole_video=len(info.video_files) == 1,
        )

    def bundled_subtitles(self) -> tuple[TorrentFile, ...]:
        """Sidecars being fetched for the active file (empty if none apply)."""
        return self._subtitle_files

    def completed_bundled_subtitles(self) -> list[Path]:
        """On-disk paths of the active file's sidecars that finished downloading.

        A partially-written subtitle would hand mpv a truncated file, so a
        sidecar has to be both fully downloaded *and* fully on disk — piece
        completion runs slightly ahead of libtorrent's disk thread. Safe to
        call from any thread.
        """
        h = self._handle
        if h is None or not self._subtitle_files:
            return []
        try:
            progress = h.file_progress()
        except RuntimeError:
            return []  # handle removed under us (raced with cleanup)

        cache = _cache_dir()
        done: list[Path] = []
        for sub in self._subtitle_files:
            if sub.index >= len(progress) or progress[sub.index] < sub.size:
                continue
            path = cache / sub.sub_path
            try:
                if path.stat().st_size >= sub.size:
                    done.append(path)
            except OSError:
                continue  # not written out yet
        return done

    def current_info(self) -> TorrentInfo | None:
        """Return the open torrent's info, or None if nothing is open."""
        return self._info

    def current_file_index(self) -> int:
        """Return the active file's index in the open torrent, or -1."""
        return self._file_index

    def current_file_path(self) -> Path | None:
        """Return the active file's path in the torrent cache, or None.

        The file is sparse while the torrent streams, so this is only good for
        reads that stay inside what has already landed.
        """
        return Path(self._file_path) if self._file_path else None

    def live_status(self) -> TorrentStatus | None:
        """Snapshot the open torrent's transfer stats, or None if nothing's open.

        Safe to call from any thread (libtorrent's ``status()`` is). Returns
        None if the handle was removed under us (raced with ``cleanup()``).
        """
        h = self._handle
        if h is None:
            return None
        try:
            s = h.status()
            total_done = self._selected_file_done(h)
        except RuntimeError:
            return None
        return TorrentStatus(
            download_rate=s.download_rate,
            upload_rate=s.upload_rate,
            num_peers=s.num_peers,
            num_seeds=s.num_seeds,
            total_done=total_done,
            total_wanted=self._file_size,
        )

    def _selected_file_done(self, h: lt.torrent_handle) -> int:
        """Return downloaded bytes for the currently selected file."""
        if (
            self._file_index < 0
            or self._file_size <= 0
            or self._piece_length <= 0
        ):
            return 0

        first_piece = self._file_offset // self._piece_length
        last_piece = (self._file_offset + self._file_size - 1) // self._piece_length
        file_start = self._file_offset
        file_end = self._file_offset + self._file_size
        done = 0

        for piece in range(first_piece, last_piece + 1):
            if not h.have_piece(piece):
                continue
            piece_start = piece * self._piece_length
            piece_end = piece_start + self._piece_length
            overlap_start = max(file_start, piece_start)
            overlap_end = min(file_end, piece_end)
            done += max(0, overlap_end - overlap_start)

        return min(done, self._file_size)

    def pieces_for_range(self, file_offset: int, length: int) -> range:
        """Return piece indices covering a byte range within the video file.

        Returns an empty range once the engine has been cleaned up so a late
        HTTP handler reads straight from whatever's on disk without waiting.
        """
        if self._piece_length == 0 or self._handle is None:
            return range(0, 0)
        abs_start = self._file_offset + file_offset
        abs_end = self._file_offset + file_offset + length - 1
        first = abs_start // self._piece_length
        last = abs_end // self._piece_length
        return range(first, last + 1)

    def wait_for_piece(self, piece_index: int, timeout: float = _PIECE_WAIT_TIMEOUT) -> bool:
        """Block until a piece is downloaded. Returns False on timeout or once
        the engine has been cleaned up (handle gone, race with `cleanup()`)."""
        h = self._handle
        if h is None:
            return False
        try:
            if h.have_piece(piece_index):
                return True
            h.set_piece_deadline(piece_index, 0)
            deadline = time.monotonic() + timeout
            while not h.have_piece(piece_index):
                if self._handle is None:
                    return False
                if time.monotonic() > deadline:
                    log.warning("timed out waiting for piece %d", piece_index)
                    return False
                time.sleep(0.1)
            return True
        except RuntimeError as exc:
            # libtorrent raises "invalid torrent handle used" if the handle is
            # removed under us (typically: cleanup() ran between our None check
            # and the next have_piece call).
            log.info("torrent handle gone during wait_for_piece: %s", exc)
            return False

    def _save_resume_data(self) -> None:
        """Persist resume data (including the info-dict) for the open torrent.

        Best-effort: keyed by info-hash in the cache dir, this lets the next
        ``open_torrent`` skip the metadata fetch and the full piece re-hash.
        Blocks briefly waiting for libtorrent's alert; never raises.
        """
        h, ses = self._handle, self._session
        if h is None or ses is None or not self._info_hash:
            return
        try:
            if not h.status().has_metadata:
                return  # nothing worth saving yet
            h.save_resume_data(lt.torrent_handle.save_info_dict)
            deadline = time.monotonic() + _RESUME_SAVE_TIMEOUT
            while time.monotonic() < deadline:
                ses.wait_for_alert(200)
                for a in ses.pop_alerts():
                    if isinstance(a, lt.save_resume_data_alert):
                        path = _resume_path(self._info_hash)
                        path.write_bytes(lt.write_resume_data_buf(a.params))
                        log.info("saved resume data: %s", path)
                        return
                    if isinstance(a, lt.save_resume_data_failed_alert):
                        log.info("resume data save skipped: %s", a.message())
                        return
            log.info("timed out saving resume data for %s", self._info_hash)
        except Exception:
            log.exception("failed to save resume data")

    def cleanup(self) -> None:
        """Stop serving and remove the torrent from the session (files stay cached)."""
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._save_resume_data()
        if self._handle is not None and self._session is not None:
            try:
                self._session.remove_torrent(self._handle)
            except Exception:
                log.exception("failed to remove torrent")
            self._handle = None
        self._info = None
        self._info_hash = None
        self._file_index = -1
        self._piece_length = 0
        self._file_offset = 0
        self._file_size = 0
        self._file_path = ""
        self._subtitle_files = ()

    def shutdown(self) -> None:
        """Tear down the engine completely (call on app exit)."""
        self.cleanup()
        self._session = None


_engine: TorrentEngine | None = None


def get_engine() -> TorrentEngine:
    global _engine
    if _engine is None:
        _engine = TorrentEngine()
    return _engine


def cleanup_engine() -> None:
    if _engine is not None:
        _engine.cleanup()


def engine_status() -> TorrentStatus | None:
    """Live transfer stats for the engine's open torrent, or None."""
    if _engine is None:
        return None
    return _engine.live_status()


def shutdown_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.shutdown()
        _engine = None
