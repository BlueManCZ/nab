"""Retime a scraped subtitle to the release actually being played.

A subtitle matched by release name is routinely timed for a *different cut* of
the same episode — a longer recap, an extra distributor card, a PAL transfer —
and plays seconds or minutes out of step. The release itself always knows the
right timing: whatever subtitle tracks it shipped with mark exactly when
someone speaks.

Only *when* matters here, never *what*, so a bitmap track (VobSub, PGS) makes
just as good a reference as a text one — ffprobe reports packet timestamps
without anything having to read the pictures. Lining the two event lists up is
then one-dimensional: vote every plausible pairwise difference into a
histogram and take the peak. Anything short of a convincing peak leaves the
file untouched, on the grounds that a confidently wrong retime is worse than
the drift it would replace.
"""

import json
import logging
import shutil
import subprocess
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from statistics import median

from nab.resolver import ResolvedSource, SourceType

log = logging.getLogger(__name__)

# Only the head of the video is probed: it keeps the read cheap on a large
# file and, for a torrent still downloading, stays inside the part that has
# actually landed (libtorrent streams sequentially from the front).
_PROBE_WINDOW = 1200.0
_PROBE_TIMEOUT = 60.0

# Histogram resolution. Finer than a frame would only sharpen noise — the
# peak gets refined against the real residuals afterwards anyway.
_VOTE_STEP = 0.05
# Widest shift worth considering. Beyond a few minutes a "match" is much more
# likely to be two unrelated dialogue rhythms lining up by chance.
_MAX_OFFSET = 300.0
_MATCH_TOLERANCE = 0.25

# Framerate conversions that change a subtitle's *pace*, not just its start.
# Unity first: it is the incumbent every other scale has to beat.
_SCALES = (1.0, 25 / 24, 24 / 25, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24)
# How much better a rescale must be than a plain shift before we believe it.
_SCALE_MARGIN = 1.25

_MIN_EVENTS = 20
_MIN_MATCH_RATE = 0.45
_MIN_LIFT = 0.15
_MIN_SHIFT = 0.25

# The pristine download, kept beside the retimed file. Deliberately not a
# subtitle extension, so mpv's `--sub-auto` walks straight past it.
_BACKUP_SUFFIX = ".orig"


@dataclass(frozen=True, slots=True)
class Alignment:
    """How a subtitle's timeline maps onto the release's: ``t*scale+offset``."""

    scale: float
    offset: float
    match_rate: float
    baseline_rate: float

    def __str__(self) -> str:
        pace = "" if self.scale == 1.0 else f", pace x{self.scale:.5f}"
        return (
            f"{self.offset:+.2f}s{pace} "
            f"({self.baseline_rate:.0%} -> {self.match_rate:.0%} of events aligned)"
        )


# ------------------------------------------------------------------- ffprobe


@cache
def _ffprobe() -> str | None:
    exe = shutil.which("ffprobe")
    if exe is None:
        log.info("ffprobe not on PATH; subtitle sync unavailable")
    return exe


def _probe(*args: str) -> str | None:
    """Run ffprobe and return stdout, or None if it isn't usable."""
    exe = _ffprobe()
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed: %s", exc)
        return None
    return proc.stdout


@dataclass(frozen=True, slots=True)
class _Stream:
    index: int
    language: str | None
    forced: bool


def _subtitle_streams(video: Path) -> list[_Stream]:
    out = _probe(
        "-select_streams", "s",
        "-show_entries", "stream=index:stream_disposition=forced:stream_tags=language",
        "-of", "json",
        str(video),
    )
    if not out:
        return []
    try:
        streams = json.loads(out).get("streams") or []
    except json.JSONDecodeError:
        log.warning("could not parse ffprobe stream list for %s", video.name)
        return []
    return [
        _Stream(
            index=int(s["index"]),
            language=(s.get("tags") or {}).get("language"),
            forced=bool((s.get("disposition") or {}).get("forced")),
        )
        for s in streams
        if "index" in s
    ]


def _alpha3(code: str) -> str | None:
    """ISO 639-1 as the config writes it -> the 639-2 code Matroska stores."""
    try:
        from babelfish import Language

        return Language.fromietf(code).alpha3
    except Exception:
        return None


def _pick_stream(streams: Sequence[_Stream], languages: Sequence[str]) -> _Stream | None:
    """Choose the track whose event boundaries best mirror the subtitle's.

    Any track in a release carries the same cut's timing, so this is only
    about correspondence quality: same language splits its lines at the same
    places, and a forced track — signs and foreign dialogue only — may hold a
    couple of dozen events for a whole episode.
    """
    if not streams:
        return None
    wanted = {a3 for code in languages if (a3 := _alpha3(code))}
    full = [s for s in streams if not s.forced] or list(streams)
    return next((s for s in full if s.language in wanted), full[0])


def _stream_event_starts(video: Path, index: int) -> list[float]:
    out = _probe(
        "-select_streams", str(index),
        "-read_intervals", f"%+{_PROBE_WINDOW:.0f}",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        str(video),
    )
    if not out:
        return []
    starts = set()
    for line in out.splitlines():
        try:
            starts.add(float(line.strip().rstrip(",")))
        except ValueError:
            continue  # ffprobe emits N/A for packets with no timestamp
    return sorted(starts)


# ------------------------------------------------------------ subtitle files


def _load(path: Path):
    """Parse a subtitle file, tolerating the encodings providers hand out."""
    import pysubs2

    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return pysubs2.SSAFile.from_string(text)
    raise ValueError(f"could not decode {path.name}")


def _file_event_starts(path: Path) -> list[float]:
    try:
        subs = _load(path)
    except Exception:
        log.warning("could not read subtitle %s", path.name, exc_info=True)
        return []
    return sorted(ev.start / 1000 for ev in subs if not ev.is_comment)


# ---------------------------------------------------------------- alignment


def _nearest(ref: Sequence[float], t: float) -> float | None:
    i = bisect_left(ref, t)
    neighbours = [ref[j] for j in (i - 1, i) if 0 <= j < len(ref)]
    return min(neighbours, key=lambda r: abs(r - t)) if neighbours else None


def _match_rate(ref: Sequence[float], cand: Sequence[float], scale: float, offset: float) -> float:
    """Fraction of events that land on a reference event once retimed.

    Only events falling inside the reference's own span are scored: the
    reference covers the head of the file, and everything past it is unjudged
    rather than wrong. Dividing by the smaller of the two populations keeps a
    sparse reference from capping the score.
    """
    lo, hi = ref[0] - _MATCH_TOLERANCE, ref[-1] + _MATCH_TOLERANCE
    shifted = [t * scale + offset for t in cand]
    comparable = [t for t in shifted if lo <= t <= hi]
    if not comparable:
        return 0.0
    matched = sum(
        1
        for t in comparable
        if (r := _nearest(ref, t)) is not None and abs(r - t) <= _MATCH_TOLERANCE
    )
    return min(1.0, matched / min(len(comparable), len(ref)))


def _vote_offset(ref: Sequence[float], cand: Sequence[float], scale: float) -> float | None:
    """The offset the most event pairs agree on, at histogram resolution."""
    votes: Counter[int] = Counter()
    for t in cand:
        shifted = t * scale
        lo = bisect_left(ref, shifted - _MAX_OFFSET)
        hi = bisect_right(ref, shifted + _MAX_OFFSET)
        for r in ref[lo:hi]:
            votes[round((r - shifted) / _VOTE_STEP)] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0] * _VOTE_STEP


def _refine(ref: Sequence[float], cand: Sequence[float], scale: float, offset: float) -> float:
    """Re-centre a 50 ms bucket on the median of the residuals it captured."""
    residuals = []
    for raw in cand:
        t = raw * scale + offset
        r = _nearest(ref, t)
        if r is not None and abs(r - t) <= _MATCH_TOLERANCE:
            residuals.append(r - t)
    return offset + median(residuals) if residuals else offset


def _beats(new: Alignment, best: Alignment) -> bool:
    """Higher match rate wins, except that a rescale has to earn it.

    Searching scale as well as offset has far more freedom to find a peak in
    noise, so unity keeps the tie. That way a subtitle which only needed
    shifting never gets stretched as well.
    """
    if new.scale != 1.0 and best.scale == 1.0:
        return new.match_rate > best.match_rate * _SCALE_MARGIN
    if new.scale == 1.0 and best.scale != 1.0:
        return new.match_rate * _SCALE_MARGIN >= best.match_rate
    return new.match_rate > best.match_rate


def align(ref: Sequence[float], cand: Sequence[float]) -> Alignment | None:
    """Find how `cand`'s event starts map onto `ref`'s, or None if unconvincing.

    None covers three different answers, all of which mean "leave the file
    alone": too little to go on, no offset that explains the data, and — the
    common one — a subtitle that was already in sync.
    """
    if len(ref) < _MIN_EVENTS or len(cand) < _MIN_EVENTS:
        log.info("not enough events to sync (%d reference, %d subtitle)", len(ref), len(cand))
        return None

    baseline = _match_rate(ref, cand, 1.0, 0.0)
    best: Alignment | None = None
    for scale in _SCALES:
        offset = _vote_offset(ref, cand, scale)
        if offset is None:
            continue
        offset = _refine(ref, cand, scale, offset)
        found = Alignment(
            scale=scale,
            offset=offset,
            match_rate=_match_rate(ref, cand, scale, offset),
            baseline_rate=baseline,
        )
        if best is None or _beats(found, best):
            best = found

    if best is None:
        return None
    if best.match_rate < _MIN_MATCH_RATE:
        log.info("no convincing alignment: best is %s", best)
        return None
    if best.match_rate < baseline + _MIN_LIFT:
        log.info("subtitle already tracks the release: %s", best)
        return None
    if best.scale == 1.0 and abs(best.offset) < _MIN_SHIFT:
        log.info("subtitle already in sync (%+.2fs)", best.offset)
        return None
    return best


# -------------------------------------------------------------------- apply


def _retime(path: Path, alignment: Alignment) -> bool:
    """Rewrite `path` under `alignment`, keeping the download as `<name>.orig`.

    Always retimes from the pristine download, so re-running this on an
    already-synced file corrects it rather than compounding the shift.
    """
    original = path.with_name(path.name + _BACKUP_SUFFIX)
    source = original if original.is_file() else path
    try:
        subs = _load(source)
    except Exception:
        log.warning("could not retime %s", path.name, exc_info=True)
        return False

    offset_ms = alignment.offset * 1000
    for ev in subs:
        ev.start = round(ev.start * alignment.scale + offset_ms)
        ev.end = round(ev.end * alignment.scale + offset_ms)
    # A backwards shift can push the opening lines before the start of the
    # file; clip what still overlaps it and drop what doesn't.
    subs.events = [ev for ev in subs.events if ev.end > 0]
    for ev in subs.events:
        ev.start = max(ev.start, 0)

    try:
        if not original.is_file():
            shutil.copy2(path, original)
        subs.save(str(path), encoding="utf-8")
    except OSError:
        log.warning("could not write retimed %s", path.name, exc_info=True)
        return False
    return True


# ---------------------------------------------------------------- reference


def _bundled_references(source: ResolvedSource) -> list[Path]:
    """Sidecars the torrent itself shipped — already timed to this release."""
    if not source.source_type.is_torrent:
        return []
    from nab.torrent import is_available

    if not is_available():
        return []
    from nab.torrent.engine import get_engine

    try:
        return get_engine().completed_bundled_subtitles()
    except Exception:
        log.warning("could not list bundled subtitles", exc_info=True)
        return []


def _video_path(source: ResolvedSource) -> Path | None:
    """The video on disk. For a torrent that's the torrent cache, not the
    HTTP URL mpv streams — probing the URL would pull pieces we don't need."""
    if source.source_type is SourceType.LOCAL_FILE:
        return Path(source.playable_url)
    if not source.source_type.is_torrent:
        return None
    from nab.torrent import is_available

    if not is_available():
        return None
    from nab.torrent.engine import get_engine

    return get_engine().current_file_path()


def reference_starts(source: ResolvedSource, languages: Sequence[str] = ()) -> list[float]:
    """Event start times that define correct timing for `source`, or []."""
    for path in _bundled_references(source):
        starts = _file_event_starts(path)
        if len(starts) >= _MIN_EVENTS:
            log.info("sync reference: bundled %s (%d events)", path.name, len(starts))
            return starts

    video = _video_path(source)
    if video is None or not video.is_file():
        return []
    stream = _pick_stream(_subtitle_streams(video), languages)
    if stream is None:
        log.info("%s has no subtitle track to sync against", video.name)
        return []
    starts = _stream_event_starts(video, stream.index)
    log.info(
        "sync reference: %s stream %d (%s, %d events in first %.0fs)",
        video.name, stream.index, stream.language or "und", len(starts), _PROBE_WINDOW,
    )
    return starts


# ----------------------------------------------------------------- entry point


def sync_to_release(
    paths: Sequence[Path], source: ResolvedSource, languages: Sequence[str] = ()
) -> list[Path]:
    """Retime `paths` in place to match `source`'s own timing.

    Returns the same paths either way — the files are rewritten where they
    sit, with the download preserved alongside as `<name>.orig` — so callers
    hand mpv one list regardless of what happened here. Best-effort
    throughout: an unreadable file, a release with nothing to reference, or an
    alignment we can't stand behind all leave the subtitle exactly as it came.

    Blocking (ffprobe): call from a worker thread.
    """
    paths = list(paths)
    if not paths or _ffprobe() is None:
        return paths

    ref = reference_starts(source, languages)
    if len(ref) < _MIN_EVENTS:
        return paths

    for path in paths:
        original = path.with_name(path.name + _BACKUP_SUFFIX)
        cand = _file_event_starts(original if original.is_file() else path)
        alignment = align(ref, cand)
        if alignment is None:
            continue
        if _retime(path, alignment):
            log.info("retimed %s by %s", path.name, alignment)
    return paths


__all__ = ["Alignment", "align", "reference_starts", "sync_to_release"]
