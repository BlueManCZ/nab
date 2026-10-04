"""
Detect a "series" — a group of similarly-named files that order themselves
along a numeric axis (TV episodes, parts numbered 1..N, tracks 01..NN, etc).

Pure functions over filenames; no filesystem I/O. The caller supplies the
sibling list, so the same logic serves local-file directories today and
the magnet/torrent file list later.
"""
import re
from dataclasses import dataclass

# Season/episode tokens, most explicit form first. Each yields (season,
# episode). The `s`-form deliberately has no left word boundary so
# `ShowS01E01.mkv` parses; the `x`-form uses digit lookarounds so a
# resolution like `1920x1080` can't masquerade as 19x10.
_SEASON_EPISODE_PATTERNS = (
    re.compile(
        r"s(?:eason)?[\s._-]*(\d{1,2})[\s._-]*e(?:p(?:isode)?)?[\s._-]*(\d{1,3})\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)"),
)


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

    Pass 0 reads season/episode tokens (`S01E02`, `1x02`, `Season 1
    Episode 2`) and, when 2+ files carry one, orders every file by
    (season, episode). This is the only pass that spans seasons: the
    name-shape passes below track a *single* varying number, so on a
    complete-series torrent they can only ever describe one season — or,
    when episode titles repeat across seasons (`S01E01.Episode.1` vs
    `S02E01.Episode.1`), latch onto the season digit and collect just the
    season premieres.

    The remaining passes handle everything that isn't numbered by season
    and episode. For each numeric run in `current`, build a regex
    anchoring the surrounding text and count siblings that match:

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
    return (
        _season_episode_view(current, siblings)
        or _try_axis(current, siblings, strict=True)
        or _try_axis(current, siblings, strict=False)
    )


def _parse_season_episode(
    pattern: re.Pattern[str], name: str
) -> tuple[int, int] | None:
    m = pattern.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _season_episode_view(
    current: str, siblings: list[str]
) -> SeriesView | None:
    """Order files by their (season, episode) tokens, across seasons.

    Returns None unless `current` and at least one sibling carry tokens of
    the same form and they don't all name the same episode. `number` is the
    episode number, so it restarts at each season boundary — it labels the
    item, while `items` order is what navigation follows.
    """
    for pattern in _SEASON_EPISODE_PATTERNS:
        current_key = _parse_season_episode(pattern, current)
        if current_key is None:
            continue

        group = [(current_key, current)]
        for sib in siblings:
            key = _parse_season_episode(pattern, sib)
            if key is not None:
                group.append((key, sib))
        if len({key for key, _ in group}) < 2:
            continue

        # Same episode in two qualities sorts by name, so the pair at least
        # stays adjacent rather than interleaving with its neighbours.
        group.sort()
        items = tuple(
            SeriesItem(name=name, number=episode)
            for (_season, episode), name in group
        )
        current_index = next(
            i for i, it in enumerate(items) if it.name == current
        )
        return SeriesView(items=items, current_index=current_index)

    return None


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
