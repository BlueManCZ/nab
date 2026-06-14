"""Local HTTP server with byte-range support for streaming torrent files to mpv."""

import http.server
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from nab.torrent.engine import TorrentEngine

log = logging.getLogger(__name__)

_CHUNK_SIZE = 256 * 1024


class _StreamServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    file_path: str
    file_size: int
    engine: "TorrentEngine"


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _StreamServer
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(self.server.file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        range_hdr = self.headers.get("Range")
        if range_hdr:
            self._serve_range(range_hdr)
        else:
            self._serve_range("bytes=0-")

    def _serve_range(self, range_hdr: str):
        file_size = self.server.file_size
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if not m:
            self.send_error(400, "Invalid Range header")
            return

        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)

        if start > end or start >= file_size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return

        length = end - start + 1

        partial = not (start == 0 and end == file_size - 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        engine = self.server.engine
        try:
            with open(self.server.file_path, "rb") as f:
                f.seek(start)
                offset = start
                remaining = length
                while remaining > 0:
                    chunk_size = min(remaining, _CHUNK_SIZE)
                    for piece in engine.pieces_for_range(offset, chunk_size):
                        if not engine.wait_for_piece(piece):
                            return
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        log.debug("http: " + format, *args)


class ByteRangeServer:
    """Serves a single file over HTTP with byte-range support on localhost."""

    def __init__(self, file_path: str, file_size: int, engine: "TorrentEngine"):
        self._server = _StreamServer(("127.0.0.1", 0), _Handler)
        self._server.file_path = file_path
        self._server.file_size = file_size
        self._server.engine = engine
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        name = Path(self._server.file_path).name
        return f"http://{host}:{port}/{quote(name)}"

    def start(self) -> str:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="torrent-http",
            daemon=True,
        )
        self._thread.start()
        log.info("byte-range server at %s", self.url)
        return self.url

    def stop(self):
        self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
