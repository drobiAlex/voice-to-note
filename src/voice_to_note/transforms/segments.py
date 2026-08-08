from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from ..domain import Segment

# What whisper.cpp documents its JSON to contain. It describes the output we
# expect, not output we are guaranteed, which is why reading it still checks.
WhisperOffsets = TypedDict("WhisperOffsets", {"from": int, "to": int})


class WhisperSegment(TypedDict):
    """One line of speech as the transcriber reports it, with its bounds."""

    text: str
    offsets: WhisperOffsets


class WhisperResult(TypedDict, total=False):
    """What the transcriber concluded about the recording as a whole."""

    language: str


class WhisperTranscription(TypedDict, total=False):
    """A whole transcription file: the lines, and what was detected overall."""

    transcription: list[WhisperSegment]
    result: WhisperResult


def fmt_ts(ms: int) -> str:
    """A timestamp a listener can scrub to, counted in plain minutes."""
    m, s = divmod(ms // 1000, 60)
    return f"{m:02d}:{s:02d}"


def display_name(speaker: str | None, names: dict[str, str]) -> str:
    """How a speaker is written wherever a transcript is shown. A segment the
    diarizer never attributed has no label at all, and reads as Unknown."""
    if speaker is None:
        return "Unknown"
    return names.get(speaker, speaker) or "Unknown"


def _field(source: Mapping[str, Any], key: str, index: int) -> Any:
    """Reads one field of a transcription, naming what a changed whisper output
    left out — the JSON it came from lives in a temp dir the user never sees."""
    try:
        return source[key]
    except (KeyError, TypeError) as e:
        # TypeError means the entry is not a mapping at all, e.g. a bare string
        raise ValueError(f"whisper segment {index} has no {key!r}") from e


def segments_from_whisper(raw: WhisperTranscription) -> list[Segment]:
    """Takes the usable speech out of a transcription, dropping silent stretches."""
    segs = []
    for i, s in enumerate(raw.get("transcription", [])):
        text = _field(s, "text", i).strip()
        if not text:
            continue
        offsets = _field(s, "offsets", i)
        segs.append(Segment(_field(offsets, "from", i), _field(offsets, "to", i), text))
    return segs


def display_text(segment: Segment, *, raw: bool = False) -> str:
    """What a line reads as: the repair, once one has been accepted for it, and
    otherwise the words as transcribed. Asking for raw gives back the recording's
    own words whatever has been done to them since."""
    if raw or segment.refined_text is None:
        return segment.text
    return segment.refined_text


def segments_as_dicts(
    segments: Sequence[Segment], names: dict[str, str], *, raw: bool = False
) -> list[dict]:
    """The transcript shaped for scripts to consume, speakers already named.
    Each line reads as its repair where one exists, or as transcribed when raw."""
    return [
        {
            "t0_ms": s.t0_ms,
            "t1_ms": s.t1_ms,
            "speaker": display_name(s.speaker, names),
            "text": display_text(s, raw=raw),
        }
        for s in segments
    ]


def transcript_text(
    segments: Sequence[Segment], names: dict[str, str], *, raw: bool = False
) -> str:
    """Builds the speaker-labeled transcript the LLM reads — naming quality here
    decides who gets credited with action items in the notes. Lines read as their
    repair where one exists, or as transcribed when raw."""
    # consecutive segments from the same person become one timestamped line
    lines: list[str] = []
    buf: list[str] = []
    speaker: str | None = None
    start_ms = 0
    for s in segments:
        who = display_name(s.speaker, names)
        if who != speaker:
            if buf:
                lines.append(f"[{fmt_ts(start_ms)}] {speaker}: {' '.join(buf)}")
            speaker, start_ms, buf = who, s.t0_ms, []
        buf.append(display_text(s, raw=raw))
    if buf:
        lines.append(f"[{fmt_ts(start_ms)}] {speaker}: {' '.join(buf)}")
    return "\n".join(lines)
