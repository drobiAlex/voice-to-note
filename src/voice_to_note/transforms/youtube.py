import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from ..domain import Segment


class CaptionTrack(TypedDict, total=False):
    """One caption file yt-dlp offers for a language: where to fetch it and
    what format it comes in."""

    url: str
    ext: str


class VideoInfo(TypedDict, total=False):
    """What yt-dlp documents its info JSON to contain. It describes the output
    we expect, not output we are guaranteed, which is why reading it still
    checks."""

    id: str
    title: str
    channel: str
    uploader: str
    duration: float
    upload_date: str
    webpage_url: str
    language: str
    is_live: bool
    subtitles: dict[str, list[CaptionTrack]]
    automatic_captions: dict[str, list[CaptionTrack]]


class Json3Seg(TypedDict, total=False):
    """One run of caption text inside an event."""

    utf8: str


class Json3Event(TypedDict, total=False):
    """One timed caption event in YouTube's json3 track format."""

    tStartMs: int
    dDurationMs: int
    segs: list[Json3Seg]


@dataclass(frozen=True)
class YouTubeVideo:
    """One video as the fetcher reports it: what to call the memo, when to
    date it, and which caption tracks exist to choose between."""

    video_id: str
    # the canonical watch URL — a video reaches this app as youtu.be/…,
    # /shorts/… or watch?v=…&list=…, and this one spelling is what lets a
    # video offered a second time be recognised as one already imported
    url: str
    title: str
    channel: str
    duration_s: float
    # upload day at midnight UTC in the recorded_at spelling; YouTube only
    # says the day, and empty when it did not say even that
    uploaded_at: str | None
    language: str | None
    is_live: bool
    subtitles: dict[str, list[CaptionTrack]]
    automatic_captions: dict[str, list[CaptionTrack]]


def _field(source: Mapping[str, Any], key: str) -> Any:
    """Reads one field of the info JSON, naming what a changed yt-dlp output
    left out — the JSON itself was piped through a subprocess the user never
    saw, so the name is the only trace of what went missing."""
    try:
        return source[key]
    except (KeyError, TypeError) as e:
        raise ValueError(f"yt-dlp video info has no {key!r}") from e


def _uploaded(upload_date: object) -> str | None:
    """The upload day as a comparable stamp in the one spelling recorded_at
    uses everywhere, or nothing when YouTube did not say. Midnight because the
    source only names the day, and a made-up time of day would collide with a
    real recording's stamp exactly as often as midnight does — never, except
    by an ffprobe tag stamped to the very second."""
    if not isinstance(upload_date, str) or len(upload_date) != 8 or not upload_date.isdigit():
        return None
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"


def video_from_info(info: VideoInfo) -> YouTubeVideo:
    """Takes what the importer needs out of a yt-dlp info dump. The id, title
    and canonical URL are demanded — without them the memo cannot be named or
    recognised again — while everything else degrades to empty, since a video
    with no known duration or upload day is still worth importing."""
    return YouTubeVideo(
        video_id=_field(info, "id"),
        url=_field(info, "webpage_url"),
        title=_field(info, "title"),
        channel=info.get("channel") or info.get("uploader") or "",
        duration_s=float(info.get("duration") or 0),
        uploaded_at=_uploaded(info.get("upload_date")),
        language=info.get("language"),
        is_live=bool(info.get("is_live")),
        subtitles=info.get("subtitles") or {},
        automatic_captions=info.get("automatic_captions") or {},
    )


def _match(langs: Mapping[str, Any], pref: str) -> str | None:
    """The track language a preference names: itself when it is there, else a
    regional variant of it ("en" finds "en-US"), sorted so two variants pick
    the same one on every filesystem and every run."""
    if pref in langs:
        return pref
    for lang in sorted(langs):
        if lang.startswith(pref + "-"):
            return lang
    return None


def _track_url(tracks: Sequence[CaptionTrack]) -> str | None:
    """One track's URL out of the formats offered for a language, taking the
    json3 one when it is listed — the other formats need the fmt override the
    gateway appends, which this makes a fallback instead of the whole plan."""
    for track in tracks:
        if track.get("ext") == "json3" and track.get("url"):
            return track["url"]
    for track in tracks:
        if track.get("url"):
            return track["url"]
    return None


def pick_track(
    video: YouTubeVideo, preferred: Sequence[str], any_language: bool = True
) -> tuple[str, str, bool] | None:
    """Which caption track an import should fetch: (language, url, auto?), or
    nothing when the video offers no acceptable track. Captions someone wrote
    beat auto-generated ones in *any* language — a human transcript in the
    video's own tongue reads better downstream than a machine's unpunctuated
    guess in the preferred one. Within each kind the caller's languages win in
    order; asked to take any language, the video's own comes next, then
    English, then whatever exists. A caller that says any_language=False asked
    for exactly the languages it named, and is told plainly when none of them
    is there rather than handed a surprise."""
    for tracks, auto in ((video.subtitles, False), (video.automatic_captions, True)):
        order = list(preferred)
        if any_language:
            order += [lang for lang in (video.language, "en") if lang]
        for pref in order:
            lang = _match(tracks, pref)
            if lang is not None:
                url = _track_url(tracks[lang])
                if url is not None:
                    return lang, url, auto
        if any_language:
            for lang in sorted(tracks):
                url = _track_url(tracks[lang])
                if url is not None:
                    return lang, url, auto
    return None


def caption_langs(video: YouTubeVideo) -> list[str]:
    """Every language this video has any captions in, for the error that
    names what *is* there when the one asked for is not."""
    return sorted(set(video.subtitles) | set(video.automatic_captions))


# how caption events are folded into transcript paragraphs. A paragraph runs
# until it would pass the cap, but once past the floor a real pause ends it
# early — the same reasoning as live transcription's cut_offset: a boundary
# placed in a pause cuts between thoughts, not through them
MAX_PARAGRAPH_MS = 75_000
MIN_PARAGRAPH_MS = 30_000
PAUSE_MS = 2_000


def segments_from_captions(text: str) -> list[Segment]:
    """Turns a json3 caption track into the timed paragraphs a memo stores.
    Events arrive one caption line at a time — a few words each — which read
    as noise in a transcript and drown an LLM prompt in timestamps, so they
    are grouped into paragraphs cut at pauses. Auto-generated captions carry
    no punctuation or capitalization; that is left for the model reading the
    transcript, which restores it better than any heuristic here would."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError("caption track is not json3 — YouTube may have changed formats") from e
    events: list[tuple[int, int, str]] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        words = " ".join("".join(s.get("utf8", "") for s in segs).split())
        if not words:
            continue
        t0 = int(_field(event, "tStartMs"))
        events.append((t0, t0 + int(event.get("dDurationMs") or 0), words))
    events.sort()
    paragraphs: list[Segment] = []
    buf: list[str] = []
    start = end = 0
    for t0, t1, words in events:
        pause = buf and t0 - end > PAUSE_MS and end - start >= MIN_PARAGRAPH_MS
        full = buf and t1 - start > MAX_PARAGRAPH_MS
        if pause or full:
            paragraphs.append(Segment(start, end, " ".join(buf)))
            buf = []
        if not buf:
            start = t0
        buf.append(words)
        end = max(end, t1)
    if buf:
        paragraphs.append(Segment(start, end, " ".join(buf)))
    return paragraphs
