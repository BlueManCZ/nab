"""The library as the user thinks of it: shows and movies, not torrents.

Pure functions over the stored torrents and their history. A show's seasons
often arrive as separate torrents — sometimes a season twice, from different
release groups — so releases are grouped by their cleaned-up title and
episodes merged by (season, episode), keeping whichever copy the user has
actually been watching.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import cached_property

from nab.history.model import HistoryEntry
from nab.library.naming import clean_title, episode_title, show_name, title_key
from nab.library.store import ProgressIndex, StoredTorrent
from nab.resolver.source import join_torrent_fragment
from nab.series import detect_series, find_season_episode
from nab.torrent.model import TorrentFile

_SAMPLE_RE = re.compile(r"\bsample\b", re.IGNORECASE)
# A multi-video torrent without episode numbers is still one movie when one
# file dominates it: the feature plus its featurettes.
_FEATURE_SHARE = 0.5


class Kind(Enum):
    SHOW = auto()    # episodes, usually in seasons
    MOVIE = auto()   # a single feature


@dataclass(slots=True)
class Episode:
    """One playable slot of a title — an episode, or a movie's feature."""

    input: str                 # what to play: the torrent source + #file=
    file_name: str
    season: int | None
    number: int | None
    name: str | None           # the episode's own title, if the file says
    size: int
    progress: HistoryEntry | None
    watched: bool = False
    # Every copy's input, the played one included: the same episode from
    # another release still counts as this one.
    inputs: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """`S01E04`, or just the number for an unseasoned run."""
        if self.season is not None and self.number is not None:
            return f"S{self.season:02d}E{self.number:02d}"
        return str(self.number) if self.number is not None else ""

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.number is not None:
            return f"Episode {self.number}"
        return clean_title(self.file_name).name

    @property
    def in_progress(self) -> bool:
        return not self.watched and self.progress is not None and self.progress.resumable

    @property
    def fraction(self) -> float:
        """How far through the episode the user is, 0..1."""
        if self.watched:
            return 1.0
        p = self.progress
        if p is None or not p.duration or p.duration <= 0:
            return 0.0
        return min(p.last_position / p.duration, 1.0)

    @property
    def remaining(self) -> float | None:
        p = self.progress
        if p is None or not p.duration:
            return None
        return max(p.duration - p.last_position, 0.0)

    @property
    def last_played_at(self) -> str:
        return self.progress.last_played_at if self.progress is not None else ""


@dataclass(slots=True)
class Season:
    number: int | None
    episodes: list[Episode]

    @property
    def watched_count(self) -> int:
        return sum(e.watched for e in self.episodes)


@dataclass
class Title:
    """A show or movie, assembled from one or more torrents."""

    key: str
    name: str
    year: int | None
    kind: Kind
    seasons: list[Season] = field(default_factory=list)
    extras: list[Episode] = field(default_factory=list)
    info_hashes: list[str] = field(default_factory=list)
    added_at: str = ""
    total_size: int = 0

    @cached_property
    def episodes(self) -> list[Episode]:
        """The main run, in viewing order — extras excluded."""
        return [e for s in self.seasons for e in s.episodes]

    @property
    def watched_count(self) -> int:
        return sum(e.watched for e in self.episodes)

    @property
    def fraction(self) -> float:
        if self.kind is Kind.MOVIE:
            return self.episodes[0].fraction
        return self.watched_count / len(self.episodes)

    @property
    def last_played_at(self) -> str:
        return max((e.last_played_at for e in self.episodes + self.extras), default="")

    @property
    def last_activity(self) -> str:
        return max(self.last_played_at, self.added_at)

    @property
    def started(self) -> bool:
        return any(e.watched or e.in_progress for e in self.episodes)

    @cached_property
    def next_up(self) -> Episode | None:
        """What "continue" should play, or None once the run is finished.

        Anchored on the most recently played episode: still in progress, it's
        resumed; finished, the one after it is next. Ties (a season marked
        watched in one go) go to the later episode. Nothing watched or under
        way — never played, or all marked unwatched — means the very first
        episode.
        """
        episodes = self.episodes
        if not self.started:
            return episodes[0] if episodes else None
        played = [(e.last_played_at, i) for i, e in enumerate(episodes) if e.progress]
        _, anchor = max(played)
        if not episodes[anchor].watched:
            return episodes[anchor]
        return next((e for e in episodes[anchor + 1:] if not e.watched), None)

    def episode_for(self, input: str) -> Episode | None:
        """The episode (or extra) any copy of which `input` plays."""
        return next(
            (e for e in self.episodes + self.extras if input in e.inputs), None
        )

    @property
    def season_span(self) -> str:
        numbers = [s.number for s in self.seasons if s.number is not None]
        if not numbers:
            return ""
        if len(numbers) == 1:
            return f"Season {numbers[0]}"
        if numbers == list(range(numbers[0], numbers[-1] + 1)):
            return f"Seasons {numbers[0]}–{numbers[-1]}"
        return "Seasons " + ", ".join(map(str, numbers))


# Where a file lands inside its title: a (season, episode) slot, or extras.
_SlotKey = tuple[int | None, int | None]
_EXTRAS: _SlotKey = (None, None)


def _episode(
    torrent: StoredTorrent, file: TorrentFile, slot: _SlotKey, progress: ProgressIndex
) -> Episode:
    season, number = slot
    return Episode(
        input=join_torrent_fragment(torrent.source, file.sub_path),
        file_name=file.name,
        season=season,
        number=number,
        name=episode_title(file.name),
        size=file.size,
        progress=progress.get((torrent.info_hash, file.sub_path)),
    )


def _classify_torrent(
    torrent: StoredTorrent,
) -> tuple[Kind, str, int | None, dict[TorrentFile, _SlotKey]]:
    """Decide what a torrent is and where each of its videos belongs.

    Returns ``(kind, name, year, slots)``; files slotted at ``_EXTRAS`` are
    samples, featurettes and the like.
    """
    torrent_title = clean_title(torrent.name)
    main = [f for f in torrent.files if not _SAMPLE_RE.search(f.name)]
    slots: dict[TorrentFile, _SlotKey] = {f: _EXTRAS for f in torrent.files}

    tokens = {f: m for f in main if (m := find_season_episode(f.name)) is not None}
    if tokens:
        for f, m in tokens.items():
            slots[f] = (int(m.group(1)), int(m.group(2)))
        named = next((n for f in tokens if (n := show_name(f.name))), None)
        return Kind.SHOW, named or torrent_title.name, torrent_title.year, slots

    if not main:
        main = list(torrent.files)
    largest = max(main, key=lambda f: f.size)
    if len(main) == 1 or largest.size > _FEATURE_SHARE * sum(f.size for f in main):
        slots[largest] = (None, 1)
        return Kind.MOVIE, torrent_title.name, torrent_title.year, slots

    # A numbered run without seasons (parts, lectures, an anime batch):
    # order it the way the series navigator would, else by path.
    names = [f.name for f in main]
    view = max(
        (detect_series(n, names[:i] + names[i + 1:]) for i, n in enumerate(names)),
        key=lambda v: len(v.items) if v is not None else 0,
    )
    order = [it.name for it in view.items] if view is not None else names
    by_name = {f.name: f for f in main}
    for number, name in enumerate(order, start=1):
        slots[by_name[name]] = (None, number)
    return Kind.SHOW, torrent_title.name, torrent_title.year, slots


def _pick_copy(copies: list[Episode]) -> Episode:
    """One episode out of the releases that carry it.

    The copy the user last played wins, so resuming continues the same
    file; unplayed, the newest release (last in `copies`) does.
    """
    played = [c for c in copies if c.progress is not None]
    chosen = max(played, key=lambda c: c.last_played_at) if played else copies[-1]
    chosen.watched = any(c.progress is not None and c.progress.completed for c in copies)
    chosen.inputs = tuple(c.input for c in copies)
    return chosen


def build_catalog(
    torrents: Iterable[StoredTorrent], progress: ProgressIndex
) -> list[Title]:
    """Assemble the library's titles, most recently active first."""
    titles: dict[str, Title] = {}
    slots: dict[str, dict[_SlotKey, list[Episode]]] = {}
    extras: dict[str, list[Episode]] = {}

    for torrent in sorted(torrents, key=lambda t: t.added_at):
        if not torrent.files:
            continue
        kind, name, year, file_slots = _classify_torrent(torrent)
        # Movies merge only with the same film (remakes differ by year);
        # shows merge across seasons and releases by name alone.
        key = (
            f"movie:{title_key(name)}:{year or ''}"
            if kind is Kind.MOVIE
            else f"show:{title_key(name)}"
        )
        title = titles.get(key)
        if title is None:
            title = titles[key] = Title(key=key, name=name, year=year, kind=kind)
        title.year = title.year or year
        title.info_hashes.append(torrent.info_hash)
        title.added_at = max(title.added_at, torrent.added_at)
        title.total_size += sum(f.size for f in torrent.files)
        for file, slot in file_slots.items():
            episode = _episode(torrent, file, slot, progress)
            if slot == _EXTRAS:
                episode.inputs = (episode.input,)
                episode.watched = bool(episode.progress and episode.progress.completed)
                extras.setdefault(key, []).append(episode)
            else:
                slots.setdefault(key, {}).setdefault(slot, []).append(episode)

    for key, title in titles.items():
        merged = sorted(
            (_pick_copy(copies) for copies in slots.get(key, {}).values()),
            key=lambda e: (e.season or 0, e.number or 0),
        )
        seasons: dict[int | None, Season] = {}
        for episode in merged:
            seasons.setdefault(episode.season, Season(episode.season, [])).episodes.append(episode)
        title.seasons = list(seasons.values())
        title.extras = sorted(extras.get(key, []), key=lambda e: e.file_name)

    # A torrent of nothing but samples has no main run to show.
    return sorted(
        (t for t in titles.values() if t.episodes),
        key=lambda t: t.last_activity,
        reverse=True,
    )
