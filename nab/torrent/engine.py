"""Torrent streaming engine backed by libtorrent."""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from nab import VIDEO_EXTENSIONS
from nab.series import detect_series
from nab.torrent.config import TorrentConfig
from nab.torrent.http_server import ByteRangeServer

log = logging.getLogger(__name__)

_METADATA_TIMEOUT = 60
_INITIAL_PIECES_TIMEOUT = 60
_PIECE_WAIT_TIMEOUT = 30

_INFO_HASH_RE = re.compile(r"xt=urn:btih:([a-zA-Z0-9]+)", re.IGNORECASE)

ProgressCallback = Callable[[str], None]


def _noop_progress(_msg: str) -> None:
    pass


def _format_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 10):.0f} KB"


def _cache_dir() -> Path:
    d = TorrentConfig.load().effective_cache_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _info_hash(magnet_uri: str) -> str | None:
    m = _INFO_HASH_RE.search(magnet_uri)
    return m.group(1).lower() if m else None


@dataclass(slots=True, frozen=True)
class TorrentFile:
    """One video file inside a torrent, as exposed to the rest of the app."""
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
    total_done: int     # wanted bytes downloaded so far
    total_wanted: int   # bytes we want (≈ the selected file's size)


@dataclass(slots=True, frozen=True)
class TorrentInfo:
    """Metadata about an open torrent. video_files is sorted by sub_path."""
    info_hash: str | None
    video_files: tuple[TorrentFile, ...]

    def by_index(self, index: int) -> TorrentFile | None:
        return next((f for f in self.video_files if f.index == index), None)

    def by_sub_path(self, sub_path: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.sub_path == sub_path), None)

    def by_name(self, name: str) -> TorrentFile | None:
        return next((f for f in self.video_files if f.name == name), None)


class TorrentEngine:
    """Manages a single libtorrent session for streaming magnet links.

    Lifecycle: ``open_magnet`` adds a torrent and returns its file list;
    ``select_file`` switches the active file inside the open torrent and
    starts (or restarts) the HTTP server. Re-opening the same magnet URI
    returns the cached file list without re-adding the torrent.
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

    def open_magnet(
        self,
        magnet_uri: str,
        on_progress: ProgressCallback | None = None,
    ) -> TorrentInfo:
        """Add the magnet to the session (if new) and return its video file list.

        Blocks until metadata arrives. Cached by info-hash so re-opening
        the same magnet is essentially free.
        """
        progress = on_progress or _noop_progress

        new_hash = _info_hash(magnet_uri)
        if (
            self._handle is not None
            and self._info is not None
            and new_hash is not None
            and self._info_hash == new_hash
        ):
            log.info("reusing open torrent %s", new_hash)
            return self._info

        # Different torrent (or never opened) — drop the old one.
        self.cleanup()
        ses = self._ensure_session()

        params = lt.parse_magnet_uri(magnet_uri)
        params.save_path = str(_cache_dir())
        self._handle = ses.add_torrent(params)
        self._info_hash = new_hash
        h = self._handle

        progress("Connecting to swarm…")
        log.info("waiting for torrent metadata...")
        deadline = time.monotonic() + _METADATA_TIMEOUT
        while not h.status().has_metadata:
            if time.monotonic() > deadline:
                self.cleanup()
                raise TimeoutError(
                    f"Timed out waiting for torrent metadata ({_METADATA_TIMEOUT}s). "
                    "The swarm may be dead or unreachable."
                )
            s = h.status()
            progress(f"Fetching metadata… ({s.num_peers} peers)")
            time.sleep(0.5)
        log.info("metadata received")

        ti = h.torrent_file()
        fs = ti.files()
        videos: list[TorrentFile] = []
        for i in range(fs.num_files()):
            sub_path = fs.file_path(i)
            if Path(sub_path).suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(
                    TorrentFile(index=i, sub_path=sub_path, size=fs.file_size(i))
                )

        if not videos:
            names = [fs.file_path(i) for i in range(fs.num_files())]
            self.cleanup()
            raise ValueError(
                f"No video file found in torrent. Files: {', '.join(names)}"
            )

        videos.sort(key=lambda f: f.sub_path)
        self._info = TorrentInfo(info_hash=new_hash, video_files=tuple(videos))
        log.info("torrent has %d video file(s)", len(videos))
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
            raise RuntimeError("No torrent open; call open_magnet first.")

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
        priorities[file_index] = 4
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

    def default_file_index(self, info: TorrentInfo) -> int:
        """Pick a sensible file to play when the user only gave us the magnet URI.

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

    def current_info(self) -> TorrentInfo | None:
        """Return the open torrent's info, or None if nothing is open."""
        return self._info

    def current_file_index(self) -> int:
        """Return the active file's index in the open torrent, or -1."""
        return self._file_index

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
        except RuntimeError:
            return None
        return TorrentStatus(
            download_rate=s.download_rate,
            upload_rate=s.upload_rate,
            num_peers=s.num_peers,
            num_seeds=s.num_seeds,
            total_done=s.total_done,
            total_wanted=s.total_wanted,
        )

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

    def cleanup(self) -> None:
        """Stop serving and remove the torrent from the session (files stay cached)."""
        if self._server is not None:
            self._server.stop()
            self._server = None
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
