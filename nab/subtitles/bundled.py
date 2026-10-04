"""Subtitles shipped inside the torrent itself.

A release's own sidecars are usually better synced than anything we can
scrape, so magnets try these first. The engine gives the matching files
download priority when it selects the video (see
`TorrentEngine.select_file`); this module just waits for them to land.
"""

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5


def await_bundled_subtitles(
    timeout: float, cancel: threading.Event | None = None
) -> list[Path]:
    """Block until the open torrent's matched sidecars finish downloading.

    Returns whatever landed within `timeout` — possibly a partial set, and []
    when the torrent ships none (in which case this returns immediately, so
    the scraping fallback isn't delayed). Blocking: call from a worker thread.
    """
    from nab.torrent import is_available

    if not is_available():
        return []
    from nab.torrent.engine import get_engine

    engine = get_engine()
    expected = len(engine.bundled_subtitles())
    if not expected:
        return []

    log.info("waiting for %d bundled subtitle(s), up to %.0fs", expected, timeout)
    deadline = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            return []
        paths = engine.completed_bundled_subtitles()
        if len(paths) >= expected:
            return paths
        if time.monotonic() >= deadline:
            log.info("bundled subtitles timed out: %d of %d ready", len(paths), expected)
            return paths
        time.sleep(_POLL_INTERVAL)
