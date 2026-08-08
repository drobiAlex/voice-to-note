from collections.abc import Sequence

from ..domain import Segment


def fmt_ts(ms: int) -> str:
    """A timestamp a listener can scrub to, counted in plain minutes."""
    m, s = divmod(ms // 1000, 60)
    return f"{m:02d}:{s:02d}"


def display_name(speaker: str | None, names: dict[str, str]) -> str:
    """How a speaker is written wherever a transcript is shown."""
    return names.get(speaker, speaker) or "Unknown"


def segments_from_whisper(raw: dict) -> list[Segment]:
    """Takes the usable speech out of a transcription, dropping silent stretches."""
    segs = []
    for s in raw.get("transcription", []):
        text = s["text"].strip()
        if not text:
            continue
        segs.append(Segment(s["offsets"]["from"], s["offsets"]["to"], text))
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
