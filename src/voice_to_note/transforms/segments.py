from collections.abc import Sequence

from ..domain import Segment


def fmt_ts(ms: int) -> str:
    m, s = divmod(ms // 1000, 60)
    return f"{m:02d}:{s:02d}"


def display_name(speaker: str | None, names: dict[str, str]) -> str:
    """How a speaker is written wherever a transcript is shown."""
    return names.get(speaker, speaker) or "Unknown"


def segments_from_whisper(raw: dict) -> list[Segment]:
    segs = []
    for s in raw.get("transcription", []):
        text = s["text"].strip()
        if not text:
            continue
        segs.append(Segment(s["offsets"]["from"], s["offsets"]["to"], text))
    return segs


def transcript_text(segments: Sequence[Segment], names: dict[str, str]) -> str:
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
