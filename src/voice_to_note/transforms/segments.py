from collections.abc import Sequence
from typing import Any

from ..domain import Segment


def fmt_ts(ms: int) -> str:
    """A timestamp a listener can scrub to, counted in plain minutes."""
    m, s = divmod(ms // 1000, 60)
    return f"{m:02d}:{s:02d}"


def display_name(speaker: str | None, names: dict[str, str]) -> str:
    """How a speaker is written wherever a transcript is shown."""
    return names.get(speaker, speaker) or "Unknown"


def _field(source: dict, key: str, index: int) -> Any:
    """Reads one field of a transcription, naming what a changed whisper output
    left out — the JSON it came from lives in a temp dir the user never sees."""
    try:
        return source[key]
    except (KeyError, TypeError) as e:
        # TypeError means the entry is not a mapping at all, e.g. a bare string
        raise ValueError(f"whisper segment {index} has no {key!r}") from e


def segments_from_whisper(raw: dict) -> list[Segment]:
    """Takes the usable speech out of a transcription, dropping silent stretches."""
    segs = []
    for i, s in enumerate(raw.get("transcription", [])):
        text = _field(s, "text", i).strip()
        if not text:
            continue
        offsets = _field(s, "offsets", i)
        segs.append(Segment(_field(offsets, "from", i), _field(offsets, "to", i), text))
    return segs


def segments_as_dicts(segments: Sequence[Segment], names: dict[str, str]) -> list[dict]:
    """The transcript shaped for scripts to consume, speakers already named."""
    return [
        {
            "t0_ms": s.t0_ms,
            "t1_ms": s.t1_ms,
            "speaker": display_name(s.speaker, names),
            "text": s.text,
        }
        for s in segments
    ]


def transcript_text(segments: Sequence[Segment], names: dict[str, str]) -> str:
    """Builds the speaker-labeled transcript the LLM reads — naming quality here
    decides who gets credited with action items in the notes."""
    # consecutive segments from the same person become one timestamped line
    lines: list[str] = []
    speaker, start_ms, buf = None, 0, []
    for s in segments:
        who = display_name(s.speaker, names)
        if who != speaker:
            if buf:
                lines.append(f"[{fmt_ts(start_ms)}] {speaker}: {' '.join(buf)}")
            speaker, start_ms, buf = who, s.t0_ms, []
        buf.append(s.text)
    if buf:
        lines.append(f"[{fmt_ts(start_ms)}] {speaker}: {' '.join(buf)}")
    return "\n".join(lines)
