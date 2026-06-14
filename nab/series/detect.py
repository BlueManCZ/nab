"""
Detect a "series" — a group of similarly-named files sharing one varying
numeric axis (TV episodes, parts numbered 1..N, tracks 01..NN, etc).

Pure functions over filenames; no filesystem I/O. The caller supplies the
sibling list, so the same logic serves local-file directories today and
the magnet/torrent file list later.
"""
import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SeriesItem:
    name: str
    number: int


@dataclass(slots=True, frozen=True)
class SeriesView:
    """Detected series with the current file's position in it."""

    items: tuple[SeriesItem, ...]
    current_index: int

    @property
    def prev(self) -> SeriesItem | None:
        i = self.current_index - 1
        return self.items[i] if i >= 0 else None

    @property
    def next(self) -> SeriesItem | None:
        i = self.current_index + 1
        return self.items[i] if i < len(self.items) else None


def detect_series(current: str, siblings: list[str]) -> SeriesView | None:
    """Return a SeriesView for `current` among `siblings`, or None.

    `siblings` must not include `current`. None is returned when no axis
    yields a group of 2+ files (including `current`).

    Strategy: for each numeric run in `current`, build a regex anchoring
    the surrounding text and count siblings that match. Two passes:

    1. Strict: `prefix + (\\d+) + suffix` must match the whole sibling
       filename. Catches `S01E01.mkv` vs `S01E02.mkv`, `01.mp4` vs
       `02.mp4`, etc. — no false positives.
    2. Fuzzy: `prefix + (\\d+)` must match at the start; the tail is
       free. Catches episodes whose titles differ between files
       (`...E01.Pilot.mkv` vs `...E02.Strangers.mkv`). Restricted to
       siblings with the same extension as `current` and a prefix of at
       least 3 chars to avoid grouping `1.mp4` with `99-trailer.mp4`.

    The axis with the most matches wins.
    """
    return _try_axis(current, siblings, strict=True) or _try_axis(
        current, siblings, strict=False
    )


def _try_axis(
    current: str, siblings: list[str], *, strict: bool
) -> SeriesView | None:
    best_group: list[tuple[int, str]] | None = None
    best_score: tuple[int, int] | None = None
    cur_ext = _extension(current)

    for m in re.finditer(r"\d+", current):
        prefix = current[: m.start()]
        suffix = current[m.end() :]
        if strict:
            pattern = re.compile(re.escape(prefix) + r"(\d+)" + re.escape(suffix))
        else:
            if len(prefix) < 3:
                continue
            pattern = re.compile(re.escape(prefix) + r"(\d+)")

        group: list[tuple[int, str]] = [(int(m.group()), current)]
        for sib in siblings:
            mm = pattern.fullmatch(sib) if strict else pattern.match(sib)
            if mm is None:
                continue
            if not strict and _extension(sib) != cur_ext:
                continue
            group.append((int(mm.group(1)), sib))

        # An axis where every match shares the same number isn't a varying
        # axis — it's the fixed part of the name that happens to be a digit.
        # E.g. for `show.s01e02.mkv` the season-number axis collects siblings
        # that all share `01` and is useless for navigation.
        distinct = {num for num, _ in group}
        if len(group) < 2 or len(distinct) < 2:
            continue
        score = (len(distinct), len(group))
        if best_score is None or score > best_score:
            best_group = group
            best_score = score

    if best_group is None:
        return None

    best_group.sort(key=lambda t: t[0])
    items = tuple(SeriesItem(name=n, number=num) for num, n in best_group)
    current_index = next(i for i, it in enumerate(items) if it.name == current)
    return SeriesView(items=items, current_index=current_index)


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""
