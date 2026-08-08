import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..domain import Segment

# how many lines one pass repairs: enough that the model sees a conversation
# rather than isolated fragments, few enough that it returns all of them
CHUNK_SIZE = 20
# lines shown either side of a window so a garbled word can be read in context;
# they are never rewritten in that pass, only read
CONTEXT_SEGMENTS = 3

# Below this, the reply no longer resembles what was said: that is a rewrite,
# not a repair, and the original is kept instead.
MIN_SIMILARITY = 0.6
# Length bounds catch the other way a repair stops being one — a line summarised
# down to nothing, or padded out with words nobody spoke.
MIN_LENGTH_RATIO = 0.6
MAX_LENGTH_RATIO = 1.5

# The shape a local model is held to while it decodes, so the reply cannot
# wander off before the parser ever sees it.
REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
            },
        },
    },
    "required": ["segments"],
}


@dataclass(frozen=True)
class Chunk:
    """One repair pass: the lines to fix, and the lines either side that are
    shown only so the model can tell what they were meant to say."""

    before: list[Segment]
    targets: list[Segment]
    after: list[Segment]

    @property
    def target_ids(self) -> list[int]:
        """The ids this window expects back. chunk_segments guarantees every
        target has one; a hand-built Chunk need not, so those are filtered out
        rather than trusted."""
        return [s.id for s in self.targets if s.id is not None]


def chunk_segments(segments: Sequence[Segment]) -> list[Chunk]:
    """Splits a transcript into windows small enough to repair in one request,
    each carrying the neighbouring lines it needs to make sense of its own."""
    if any(s.id is None for s in segments):
        # repairs come back keyed by id; a line without one could never be filed
        raise ValueError("cannot refine a segment with no id — it is not stored yet")
    chunks = []
    for start in range(0, len(segments), CHUNK_SIZE):
        stop = start + CHUNK_SIZE
        chunks.append(
            Chunk(
                before=list(segments[max(0, start - CONTEXT_SEGMENTS) : start]),
                targets=list(segments[start:stop]),
                after=list(segments[stop : stop + CONTEXT_SEGMENTS]),
            )
        )
    return chunks


def parse_refinements(text: str, expected_ids: Sequence[int]) -> dict[int, str]:
    """Reads a repair reply, refusing one that would quietly drop a line or add
    a line nobody asked about — either would corrupt the stored transcript."""
    text = text.strip()
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in reply: {text[:200]}")
    data = json.loads(text[start : end + 1])
    entries = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError('reply has no "segments" list')

    repaired: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry or "text" not in entry:
            raise ValueError(f"reply has a segment without an id and text: {entry!r}")
        seg_id = entry["id"]
        # an id of the wrong type would otherwise read as a line left out, which
        # sends whoever debugs it looking at the model instead of the reply.
        # bool is an int in Python, and true would quietly file against line 1
        if not isinstance(seg_id, int) or isinstance(seg_id, bool):
            raise ValueError(f"segment id is not a whole number: {seg_id!r}")
        if not isinstance(entry["text"], str):
            raise ValueError(f"segment {seg_id} came back as something other than text")
        if seg_id in repaired:
            raise ValueError(f"reply repaired segment {seg_id} more than once")
        repaired[seg_id] = entry["text"]

    expected = set(expected_ids)
    missing = sorted(expected - repaired.keys())
    if missing:
        raise ValueError(f"reply left out segments: {missing}")
    invented = sorted(repaired.keys() - expected)
    if invented:
        raise ValueError(f"reply returned segments nobody asked for: {invented}")
    return repaired


def _interior_ids(chunk: Chunk) -> set[int]:
    """The lines this window could read both sides of. The first and last of
    what it was shown had only half a sentence around them."""
    window = [*chunk.before, *chunk.targets, *chunk.after]
    surrounded = {s.id for s in window[1:-1]}
    return {t.id for t in chunk.targets if t.id in surrounded and t.id is not None}


def merge_refinements(
    chunks: Sequence[Chunk], repairs: Sequence[Mapping[int, str]]
) -> dict[int, str]:
    """Collects the windows' repairs into one text per line. Should a line come
    back from more than one window, the version from a window that could read
    both sides of it wins; ties go to the earlier window, so a given transcript
    always merges the same way."""
    merged: dict[int, str] = {}
    well_placed: dict[int, bool] = {}
    # strict: the two sequences are index-matched, so a length mismatch is a
    # caller bug that would otherwise silently drop a window's repairs
    for chunk, repaired in zip(chunks, repairs, strict=True):
        surrounded = _interior_ids(chunk)
        for seg_id, text in repaired.items():
            inside = seg_id in surrounded
            if seg_id not in merged or (inside and not well_placed[seg_id]):
                merged[seg_id] = text
                well_placed[seg_id] = inside
    return merged


def _is_repair(original: str, repaired: str) -> bool:
    """Whether a reply still resembles the line it was given. This is what keeps
    a model that starts paraphrasing, translating or summarising from quietly
    replacing what somebody actually said."""
    if SequenceMatcher(None, original, repaired).ratio() < MIN_SIMILARITY:
        return False
    ratio = len(repaired) / len(original) if original else 1.0
    return MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO


def accept_repairs(
    segments: Sequence[Segment], repairs: Mapping[int, str]
) -> tuple[dict[int, str], list[int]]:
    """The text to keep for every line, and the lines whose repair was refused.
    A refused line keeps its original wording: a transcript with known
    transcription errors is worth more than one quietly rewritten."""
    text: dict[int, str] = {}
    flagged: list[int] = []
    for s in segments:
        if s.id is None:
            continue
        repaired = repairs.get(s.id)
        if repaired is not None and not _is_repair(s.text, repaired):
            text[s.id] = s.text
            flagged.append(s.id)
        else:
            text[s.id] = s.text if repaired is None else repaired
    return text, flagged
