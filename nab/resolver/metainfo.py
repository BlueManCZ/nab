"""Fetching a .torrent metainfo file over HTTP.

A torrent link on a tracker page is just a file download, so a `.torrent` URL
resolves by pulling the metainfo into a local cache and handing the engine the
file — identical to a local `.torrent` from that point on. The cache is keyed
by URL, so replaying a history entry (or switching episodes inside the same
torrent) costs no second request.
"""

import hashlib
import logging
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from nab import __version__
from nab.paths import cache_home

log = logging.getLogger(__name__)

# A metainfo file is one hash per piece, so it runs from a few KB to a couple
# of MB. Anything beyond this isn't a torrent — most likely an error page, or
# the content itself behind a mislabelled link — so we stop rather than buffer
# it all.
_MAX_SIZE = 8 << 20
_TIMEOUT = 30
_USER_AGENT = f"nab/{__version__}"

# Every bencoded metainfo file is a dict, so it opens with `d`. Cheap way to
# reject the HTML login/error page a tracker serves in place of the torrent.
_BENCODE_DICT = b"d"


def cache_path(url: str) -> Path:
    """Where `url`'s metainfo is cached, whether or not it's there yet.

    Keyed by a digest of the URL rather than its filename: tracker download
    links are often an opaque `/download.php?id=…` that names nothing, and two
    of them must not land on the same file.
    """
    directory = cache_home() / "metainfo"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{hashlib.sha256(url.encode()).hexdigest()[:40]}.torrent"


def _download(url: str) -> bytes:
    """GET `url`, refusing anything that can't be a metainfo file.

    Raises ValueError — with a message fit for a toast — for every failure
    mode, so callers handle one exception type rather than urllib's family.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            # urllib follows redirects for us; make sure we didn't get walked
            # off http(s) onto something like a file:// URL on the way.
            if urlparse(response.url).scheme not in ("http", "https"):
                raise ValueError(f"Torrent link redirected off HTTP to {response.url}")
            # One byte past the cap, so an oversized body is detectable
            # without reading all of it.
            data = response.read(_MAX_SIZE + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"Couldn't download the torrent file ({exc.code} {exc.reason})"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Couldn't reach the torrent link: {exc.reason}") from exc
    except OSError as exc:
        raise ValueError(f"Couldn't download the torrent file: {exc}") from exc

    if len(data) > _MAX_SIZE:
        raise ValueError(f"That link is too big to be a torrent file (over {_MAX_SIZE >> 20} MB)")
    if not data.startswith(_BENCODE_DICT):
        raise ValueError(f"{url} didn't return a torrent file")
    return data


def fetch(url: str, on_progress: Callable[[str], None] | None = None) -> Path:
    """Download the .torrent at `url` and return its local path.

    Served from the cache when it's already been fetched. Raises ValueError
    for anything that isn't a plausible metainfo file.
    """
    path = cache_path(url)
    if path.is_file() and path.stat().st_size:
        log.info("reusing cached metainfo for %s", url)
        return path

    if on_progress is not None:
        on_progress("Fetching torrent file…")
    log.info("downloading metainfo from %s", url)
    data = _download(url)

    # Land it through a uniquely-named temp file: a crash mid-write — or a
    # second resolve of the same URL racing this one — would otherwise leave a
    # truncated cache entry that every later run would happily reuse.
    fd, partial = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    except OSError:
        Path(partial).unlink(missing_ok=True)
        raise
    log.info("cached %d-byte metainfo at %s", len(data), path)
    return path
