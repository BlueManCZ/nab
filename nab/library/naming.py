"""Human titles out of release names.

Torrent and file names are built for trackers, not people:
``Westworld (2016) Season 4 S04 (1080p BluRay x265 HEVC 10bit AAC 7.1 Silence)``.
These pure helpers cut such a name down to what a library should show —
``Westworld``, 2016 — and pull an episode's own title out of its filename.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from nab import MEDIA_EXTENSIONS
from nab.series import find_season_episode

# A release group or site tag leading the name: `[SkT]`, `[YTS.AM]`.
_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# Everything a release name tacks on after the title. The name is cut at the
# first of these, so each must be unlikely as a word of a real title.
_TRAILER_RE = re.compile(
    r"[(\[{]"
    r"|\bs\d{1,2}(?:\s*-\s*s?\d{1,2})?\b"
    r"|\bseasons?\b|\bcomplete\b|\bseries\b"
    r"|\b(?:480|576|720|1080|2160)[pi]\b|\b4k\b|\buhd\b|\bhdr(?:10)?\b"
    r"|\bweb[- ]?(?:dl|rip)\b|\bblu[- ]?ray\b|\b(?:bd|br|dvd|hd)rip\b|\bhdtv\b"
    r"|\b[xh]\.?26[45]\b|\bhevc\b|\b10bit\b|\bremux\b",
    re.IGNORECASE,
)
_SEPARATORS = " -–—_.,:;+"
# Filenames that number an episode instead of naming it (`Episode.1`).
_GENERIC_EPISODE_RE = re.compile(r"^(?:ep(?:isode)?)?\s*\d+$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class CleanTitle:
    name: str
    year: int | None


def _strip_media_suffix(name: str) -> str:
    path = Path(name)
    return path.stem if path.suffix.lower() in MEDIA_EXTENSIONS else name


def _despace(text: str) -> str:
    """Dots and underscores as word breaks, runs of space collapsed."""
    # A dot between two letters inside a word ("Mr.Robot") is a separator
    # too; release names never mean an abbreviation there.
    text = text.replace("_", " ").replace(".", " ")
    return " ".join(text.split())


def clean_title(raw: str) -> CleanTitle:
    """Cut a release name down to its title and (optional) year.

    The year is only recognised after the first word, so a film called
    `1917` or `2012` keeps its title.
    """
    text = _despace(_LEADING_TAG_RE.sub("", _strip_media_suffix(raw)))
    year = None
    cut = len(text)
    if (m := _TRAILER_RE.search(text)) is not None and m.start() > 0:
        cut = m.start()
    for m in _YEAR_RE.finditer(text):
        if m.start() == 0:
            continue
        year = int(m.group())
        cut = min(cut, m.start())
        break
    name = text[:cut].strip(_SEPARATORS + "([{")
    return CleanTitle(name=name or text.strip() or raw, year=year)


def show_name(file_name: str) -> str | None:
    """The show a `S01E02`-style filename belongs to, or None if it doesn't say."""
    m = find_season_episode(file_name)
    if m is None:
        return None
    name = clean_title(file_name[: m.start()]).name if m.start() else ""
    return name or None


def episode_title(file_name: str) -> str | None:
    """The episode's own title from its filename, or None if it has none.

    `Westworld S03E08 Crisis Theory  [2160p x265].mkv` → `Crisis Theory`.
    Placeholder titles like `Episode.1` count as none.
    """
    stem = _strip_media_suffix(file_name)
    m = find_season_episode(stem)
    if m is None:
        return None
    text = _despace(stem[m.end():]).strip(_SEPARATORS)
    if (cut := _TRAILER_RE.search(text)) is not None:
        text = text[: cut.start()]
    if (year := _YEAR_RE.search(text)) is not None:
        text = text[: year.start()]
    text = text.strip(_SEPARATORS)
    if not text or _GENERIC_EPISODE_RE.fullmatch(text):
        return None
    return text


def title_key(name: str) -> str:
    """Grouping key under which releases of the same title collide.

    Case, accents and punctuation differ between release groups
    (`Virtù e Fortuna` / `Virtu e Fortuna`), so none of them count.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in decomposed if c.isalnum())
