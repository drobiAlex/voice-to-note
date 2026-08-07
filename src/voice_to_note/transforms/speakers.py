from collections.abc import Iterable, Sequence
from dataclasses import replace

import numpy as np

from ..domain import Segment, Speaker, SpeakerMatch, Turn

# the pipeline works on 16 kHz mono audio throughout
SAMPLES_PER_MS = 16
# how much of a speaker's audio goes into their voice fingerprint
FINGERPRINT_BUDGET_MS = 30_000
# below this a chunk is too short to embed reliably
MIN_CHUNK_SAMPLES = 8000


def turns_from_clusters(clusters: Iterable[tuple[float, float, int]]) -> list[Turn]:
    # cluster ids are arbitrary; relabel S1, S2 … in order of first appearance
    order: dict[int, int] = {}
    turns = []
    for start_s, end_s, cluster in clusters:
        if cluster not in order:
            order[cluster] = len(order) + 1
        turns.append(Turn(int(start_s * 1000), int(end_s * 1000), f"S{order[cluster]}"))
    return turns


def assign_speakers(segments: Sequence[Segment], turns: Sequence[Turn]) -> list[Segment]:
    # max-overlap wins; segments in gaps fall back to the nearest turn boundary
    out = []
    for s in segments:
        best, best_ov = None, 0
        for t in turns:
            ov = min(s.t1_ms, t.end_ms) - max(s.t0_ms, t.start_ms)
            if ov > best_ov:
                best, best_ov = t.speaker, ov
        if best is None and turns:
            mid = (s.t0_ms + s.t1_ms) // 2
            nearest = min(
                turns,
                key=lambda t: min(abs(mid - t.start_ms), abs(mid - t.end_ms)),
            )
            best = nearest.speaker
        out.append(replace(s, speaker=best))
    return out


def sample_range(turn: Turn) -> tuple[int, int]:
    return turn.start_ms * SAMPLES_PER_MS, turn.end_ms * SAMPLES_PER_MS


def select_fingerprint_turns(turns: Sequence[Turn], total_samples: int) -> list[Turn]:
    """Which of one speaker's turns to embed, longest first.

    Longest turns carry the most voice; the budget keeps a long memo cheap.
    A turn that runs past the end of the audio yields only what was recorded,
    so it is measured by that and, if too short, costs nothing from the budget.
    """
    chosen: list[Turn] = []
    used_ms = 0
    for t in sorted(turns, key=lambda t: t.end_ms - t.start_ms, reverse=True):
        if used_ms >= FINGERPRINT_BUDGET_MS:
            break
        start, end = sample_range(t)
        if min(end, total_samples) - min(start, total_samples) < MIN_CHUNK_SAMPLES:
            continue
        chosen.append(t)
        used_ms += t.end_ms - t.start_ms
    return chosen


def unit(v: np.ndarray) -> np.ndarray:
    """Scale to length 1, which is what makes a dot product a cosine."""
    return v / (np.linalg.norm(v) or 1.0)


def average_embedding(embeddings: Sequence[np.ndarray]) -> np.ndarray:
    return unit(np.mean(embeddings, axis=0))


def match_known_speakers(
    embeddings: dict[str, np.ndarray],
    pool: dict[str, list[np.ndarray]],
    threshold: float,
) -> dict[str, SpeakerMatch]:
    # compare against named speakers from other memos; best cosine wins.
    # everything is put on the unit sphere here rather than trusting callers:
    # otherwise a long vector outscores a similar-sounding voice.
    if not pool:
        return {}
    centroids = {
        name: average_embedding([unit(v) for v in vs]) for name, vs in pool.items()
    }
    matches = {}
    for label, v in embeddings.items():
        query = unit(v)
        name, sim = max(
            ((n, float(np.dot(query, c))) for n, c in centroids.items()),
            key=lambda x: x[1],
        )
        if sim >= threshold:
            matches[label] = SpeakerMatch(name, sim)
    return matches


def resolve_speaker_names(
    labels: Sequence[str],
    embeddings: dict[str, np.ndarray],
    matches: dict[str, SpeakerMatch],
    keep_names: dict[str, str],
) -> list[Speaker]:
    # a name given by hand outranks anything voice matching suggests
    return [
        Speaker(
            label,
            keep_names.get(label) or (matches[label].name if label in matches else None),
            embeddings.get(label),
        )
        for label in labels
    ]


def auto_named(
    matches: dict[str, SpeakerMatch], keep_names: dict[str, str]
) -> list[tuple[str, SpeakerMatch]]:
    return [(label, m) for label, m in matches.items() if not keep_names.get(label)]
