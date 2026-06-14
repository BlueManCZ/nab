import logging

from nab.resolver.source import ResolvedSource, SourceType

log = logging.getLogger(__name__)

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "extract_flat": False,
}


def extract(url: str) -> ResolvedSource:
    """Pull title and duration via yt-dlp, hand the original URL to mpv.

    mpv has yt-dlp built into its scripting layer (`ytdl_hook.lua`) and will
    re-resolve the URL itself. This is the only path that consistently merges
    YouTube's separate video+audio streams.
    """
    import yt_dlp

    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False, process=False)

    if info is None:
        raise RuntimeError("yt-dlp returned no info")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        first = next(iter(entries), None)
        if first is None:
            raise RuntimeError("yt-dlp returned an empty playlist")
        info = first

    title = info.get("title")
    duration = info.get("duration")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None

    log.info("yt-dlp metadata for %r -> %s", url, title)
    return ResolvedSource(
        source_type=SourceType.WEB_URL,
        playable_url=url,
        title=title,
        duration=duration,
        original_input=url,
    )
