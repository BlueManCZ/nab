# Nab

A native GTK4/libadwaita app for watching local files, web URLs (YouTube etc.),
and magnet links. Nab resolves and streams each source — running its own torrent
engine for magnets — then drives mpv, which handles the actual playback in its own
window. Everything you nab gets recorded in history.

## Architecture in one paragraph

mpv runs as a **subprocess in its own window** with `--vo=gpu-next`, so HDR works on
Wayland and X11 compositors that support it. Nab's GTK4 / libadwaita window is a
*controller* — input bar, transport controls, history sidebar — and talks to mpv over a
JSON IPC socket. We do not embed mpv into the GTK widget: mainline mpv has no Wayland
support for `--wid` (upstream issue [#9654][9654], open since 2021), and the libmpv
render API would cost us `gpu-next`/HDR (issue [#6575][6575], open since 2019). The
honest trade is two windows on screen but real HDR. If embedded HDR ever becomes
non-negotiable we replace mpv with a `FFmpeg + libplacebo` in-process pipeline; that's
a multi-month rewrite and is deferred.

[9654]: https://github.com/mpv-player/mpv/issues/9654
[6575]: https://github.com/mpv-player/mpv/issues/6575

## Status: v0.1 prototype

What works:
- GTK4 + libadwaita main window with input bar, transport controls, history sidebar
- mpv runs as a subprocess, controlled via JSON IPC over a Unix socket
- Local files and web URLs (yt-dlp) play, with title and duration shown
- Watch history persists to SQLite at `$XDG_DATA_HOME/nab/history.db`
- Resume position is updated every 5 seconds during playback
- Automatic subtitle discovery for local files and magnets (via `subliminal`,
  free providers by default). Torrents that ship their own subtitles use those
  instead. Configure languages and optional OpenSubtitles credentials in
  `$XDG_CONFIG_HOME/nab/config.toml` (see [Subtitles](#subtitles) below).
- Magnet / torrent streaming (via `libtorrent`): pieces are streamed to mpv
  over a local byte-range HTTP server as they arrive. The cache directory is
  configurable in Preferences.
- Series navigation: a prev/next + episode picker appears for on-disk series
  (`S01E01`, `01`, …) and multi-file torrents.

`libtorrent` and `subliminal` are optional — Nab degrades gracefully when they
aren't installed (magnet links and subtitle discovery are simply disabled). See
the `torrent` / `subtitles` extras in `pyproject.toml`.

Slated for next iterations:
- Thumbnails, fullscreen control auto-hide, keyboard shortcuts
- Proper packaged install (system-wide .desktop + AppStream metainfo + icon)
- Flatpak manifest + AUR PKGBUILD + Gentoo ebuild

## Run

Two things must come from the system: the **mpv** binary and the **GTK4 /
libadwaita** native libraries (which pull in cairo and gobject-introspection —
PyGObject and pycairo build and link against them). On Gentoo:

```sh
emerge media-video/mpv dev-libs/libadwaita
```

Everything else is Python and managed by [uv](https://docs.astral.sh/uv/).
`uv sync` builds PyGObject and pycairo from source against those system libraries:

```sh
uv sync                  # core dependencies
uv sync --all-extras     # also magnets (libtorrent) and subtitles (subliminal)
```

Then launch from the repo root:

```sh
uv run nab                                          # opens the controller window
uv run nab /path/to/file.mp4                        # autoplay a local file
uv run nab "https://www.youtube.com/watch?v=..."    # autoplay a URL
```

## Open from a file manager

For the dev checkout there's a per-user `.desktop` entry. After `uv sync`, run once:

```sh
uv run scripts/install-user-desktop.py
```

This drops `io.github.bluemancz.nab.desktop` into `~/.local/share/applications/`
with an `Exec` line that runs the project's uv venv (`.venv/bin/python -m nab`),
claims the common video/audio MIME types plus `x-scheme-handler/magnet`, and
refreshes the desktop database. Nab then shows up in the file manager's "Open With" list and
in app launchers. Re-run after moving the repo. Remove with `rm
~/.local/share/applications/io.github.bluemancz.nab.desktop`.

## Layout

```
nab/
├── __init__.py           version, APP_ID, media extension sets
├── __main__.py           CLI entry point (python3 -m nab)
├── application.py        Adw.Application subclass
├── window.py             Main window (controller)
├── preferences.py        Adw.PreferencesWindow (torrent cache dir)
├── paths.py              XDG base-directory helpers
├── config.py             config.toml read/write helpers
├── gtkutil.py            small shared GTK helpers (file-dialog error logging)
├── player.py             Player: spawns mpv with HDR flags + JSON IPC via python-mpv-jsonipc
├── resolver/
│   ├── source.py         Detection: local / magnet / web / direct
│   └── ytdlp.py          yt-dlp wrapper
├── series/
│   ├── detect.py         Pure filename-based series detection
│   └── nav.py            Series prev/next + episode-picker widget
├── subtitles/
│   ├── bundled.py        Sidecars shipped inside a torrent
│   ├── config.py         User preferences (TOML)
│   ├── discover.py       Subliminal-based discovery + sub-add via IPC
│   ├── panel.py          Re-fetch-subtitles button widget
│   └── sync.py           Retimes scraped subtitles to the release
├── history/
│   ├── database.py       SQLite schema + upsert / update / list
│   ├── model.py          HistoryEntry dataclass
│   └── sidebar.py        History-list sidebar widget
└── torrent/
    ├── engine.py         libtorrent session + sequential streaming
    ├── http_server.py    localhost byte-range server feeding mpv
    ├── stats_view.py     live transfer-stats dashboard widget
    └── config.py         cache-dir preference (TOML)
```

## Subtitles

When you play a local file, Nab kicks off a background search via
[subliminal](https://github.com/Diaoul/subliminal) and feeds any matches to
mpv over IPC. The `.srt` is saved next to the video file so mpv auto-loads it
on subsequent plays (no DB lookup, no Nab involvement).

Magnets work the same way, with two differences:

- **The torrent's own subtitles win.** If the release ships sidecars for the
  file you're playing, Nab gives those files download priority, waits up to 20
  seconds for them, and attaches them — they're almost always better synced
  than anything scraped. Only if the torrent has none does it fall back to the
  providers. Image-based tracks (VobSub, PGS) are skipped: they run to tens of
  megabytes and would compete with the stream.
- **Matching is on the release name, not the file.** The video in the torrent
  cache is a partially-downloaded sparse image, so its hash means nothing;
  the release name is what the free providers key on anyway. Scraped subtitles
  are saved under `$XDG_CACHE_HOME/nab/subtitles/<info-hash>/` (mpv can't
  auto-load a sidecar next to a file it's streaming over HTTP) and reused on
  replay.

Either way, the toolbar's subtitle button re-runs the provider search and
selects the first result — use it when the automatic pick is wrong.

### Sync

Providers match on release *name*, which does not pin down the release's
*cut*: a subtitle for the same episode may have been timed against a longer
recap, an extra distributor card, or a PAL transfer, and then plays seconds or
minutes out of step. So anything scraped gets retimed against the release
before it reaches mpv.

The reference is the release's own timing — a sidecar it shipped with, or
failing that its muxed subtitle tracks. Only *when* is needed, never *what*,
so an image-based track works as well as a text one: `ffprobe` reports packet
timestamps without anything having to read the pictures, over the first 20
minutes only, which keeps the read cheap and stays inside what a streaming
torrent has already downloaded. Matching the two event lists is then
one-dimensional — vote every plausible pairwise difference into a histogram,
take the peak, and check a handful of framerate ratios in case the pace is
off too.

A subtitle is only rewritten when the winning alignment explains the data far
better than leaving it alone does; noise, a mismatched episode, and a subtitle
that was already in sync all read as "no answer" and the file is left exactly
as it came. The download is kept beside it as `<name>.orig` — both as an
undo and so that re-running corrects the timing instead of compounding it.
Set `sync = false` to switch this off.

Configuration lives at `$XDG_CONFIG_HOME/nab/config.toml`. All fields are
optional:

```toml
[subtitles]
enabled = true            # set to false to disable discovery entirely
languages = ["en", "cs"]  # ISO 639-1 or 639-3; first is auto-selected
save_next_to_video = true # if false, saves to $XDG_CACHE_HOME/nab/subtitles/
sync = true               # retime scraped subtitles to the release (see Sync)

[subtitles.opensubtitles]
# Optional. Enables the opensubtitlescom provider for better movie coverage.
# Sign up at https://www.opensubtitles.com/ and put credentials here.
# username = "..."
# password = "..."
```

Free providers (Podnapisi, Gestdown, TVsubtitles) are queried by default and
require no signup. Discovery is best-effort: no match, network failure, or a
missing `subliminal` install all fail silently.
