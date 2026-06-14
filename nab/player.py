"""mpv controller built on `python-mpv-jsonipc`.

Spawns mpv in its own window with HDR-friendly flags (`--vo=gpu-next`,
`--hwdec=auto-safe`) and exposes a slim, nab-shaped surface for commands,
properties, events, and disconnect notification. The rest of the app doesn't
need to know about attribute-style property access or the underlying library's
command shape.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from python_mpv_jsonipc import MPV, MPVError

from nab.paths import config_home, runtime_dir

log = logging.getLogger(__name__)


class PlayerError(RuntimeError):
    pass


def _mpv_config_dir() -> Path:
    p = config_home() / "mpv"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Player:
    """Owns an mpv subprocess + its JSON IPC connection.

    Construction blocks until mpv is up and answering on the IPC socket
    (the library spawns mpv and queries its property/command lists before
    returning). All commands are synchronous; mpv replies over a Unix
    socket are sub-millisecond in practice.
    """

    def __init__(self, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or (runtime_dir() / f"nab-mpv-{os.getpid()}.sock")
        self._disconnect_cb: Callable[[], None] | None = None
        try:
            self._mpv: MPV | None = MPV(
                start_mpv=True,
                ipc_socket=str(self.socket_path),
                quit_callback=self._on_quit,
                force_window=True,
                keep_open=True,
                cache=True,
                cache_secs=30,
                vo="gpu-next",
                hwdec="auto-safe",
                config_dir=str(_mpv_config_dir()),
                title="Nab",
            )
        except MPVError as exc:
            raise PlayerError(str(exc)) from exc
        log.info("mpv up on ipc socket %s", self.socket_path)

    @property
    def running(self) -> bool:
        if self._mpv is None or self._mpv.mpv_process is None:
            return False
        return self._mpv.mpv_process.process.poll() is None

    def close(self) -> None:
        """Terminate mpv and tear down the IPC threads. Idempotent."""
        mpv = self._mpv
        if mpv is None:
            return
        self._mpv = None
        try:
            mpv.terminate()
        except Exception:
            log.exception("mpv terminate failed")

    # ------------------------------------------------------------------ events

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        """Register a callback fired when the socket dies (user closed mpv,
        crash, our own quit). The library auto-terminates internals when the
        socket drops, so this callback observes that — it does not need to
        clean up the underlying connection itself.
        """
        self._disconnect_cb = cb

    def observe_property(self, name: str, cb: Callable[[Any], None]) -> None:
        if self._mpv is None:
            return
        # Library callbacks receive (name, data); nab only cares about data.
        self._mpv.bind_property_observer(name, lambda _name, data: cb(data))

    def on_event(self, name: str, cb: Callable[[dict], None]) -> None:
        if self._mpv is None:
            return
        self._mpv.bind_event(name, cb)

    # --------------------------------------------------------------- commands

    def command(self, *args: Any) -> Any:
        if self._mpv is None:
            raise PlayerError("mpv not running")
        try:
            return self._mpv.command(*args)
        except MPVError as exc:
            raise PlayerError(str(exc)) from exc

    def loadfile(self, url: str, mode: str = "replace", *, start: float | None = None) -> None:
        """Tell mpv to load and play `url`.

        If `start` is given (>0), mpv begins playback at that offset once
        the file opens. Passing the position through loadfile is more
        reliable than a follow-up ``set_property("time-pos", ...)`` —
        a separate seek issued before mpv has the file open is silently
        dropped as "property unavailable".
        """
        if start is None or start <= 0:
            self.command("loadfile", url, mode)
        else:
            # The -1 is the playlist insertion index; only `replace` ignores
            # it, but mpv still requires the slot to reach the options field.
            self.command("loadfile", url, mode, -1, f"start={start}")

    def add_subtitle(self, path: str, *, select: bool = False) -> None:
        flags = "select" if select else "auto"
        self.command("sub-add", path, flags)

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def quit(self) -> None:
        try:
            self.command("quit")
        except Exception:
            pass

    # ---------------------------------------------------------------- internal

    def _on_quit(self) -> None:
        cb = self._disconnect_cb
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.exception("disconnect callback raised")


__all__ = ["Player", "PlayerError"]
