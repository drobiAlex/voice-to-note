from collections.abc import Sequence
from dataclasses import replace

import numpy as np

from ..domain import Segment, TrackFormat

# how finely a stretch of audio is measured when looking for a pause. Fine
# enough that a cut lands inside the gap between two words, coarse enough that
# measuring a minute of a meeting costs nothing worth timing
BIN_S = 0.05
# a cut is placed at the middle of the quietest stretch this long, rather than
# at the single quietest instant: the quietest instant of a word is the closure
# in the middle of it, and cutting there splits the word
QUIET_S = 0.25
# how far from the length asked for a pause may be looked for. Speech runs in
# bursts of a few seconds, so a pause is nearly always within this; searching
# wider would let chunks drift far from the length they were configured to be
SEARCH_S = 4.0

# what each sample width means as numpy reads it, for the formats a recorder
# actually writes. Anything else is refused rather than reinterpreted: the
# wrong width read as a right one is plausible noise, not an obvious failure
_INTEGER = {8: np.int8, 16: np.int16, 32: np.int32}


def mono(raw: bytes, fmt: TrackFormat) -> np.ndarray:
    """One stretch of a track as a single channel of floats, which is the only
    shape the measurements below can work on. Channels are averaged rather than
    picked from: a microphone that records one side of a stereo pair would read
    as silence if the other channel were the one taken."""
    frame = fmt.frame_bytes
    usable = len(raw) - len(raw) % frame if frame else 0
    if not usable:
        return np.zeros(0, dtype=np.float32)
    if fmt.is_float:
        if fmt.bits not in (32, 64):
            raise ValueError(f"unsupported float width: {fmt.bits}")
        flat = np.frombuffer(raw[:usable], dtype=np.float32 if fmt.bits == 32 else np.float64)
        samples = flat.astype(np.float32)
    else:
        if fmt.bits not in _INTEGER:
            raise ValueError(f"unsupported sample width: {fmt.bits}")
        flat = np.frombuffer(raw[:usable], dtype=_INTEGER[fmt.bits])
        samples = flat.astype(np.float32) / float(1 << (fmt.bits - 1))
    folded = samples.reshape(-1, fmt.channels).mean(axis=1)
    return np.asarray(folded, dtype=np.float32)


def loudness(samples: np.ndarray, rate: int, bin_s: float = BIN_S) -> np.ndarray:
    """How loud each short bin of a track is, as mean power. Power rather than
    amplitude because the bins of both sides of a meeting are added together
    below, and adding amplitudes would let a loud crackle on one side cancel
    against a quiet stretch on the other instead of ruling it out."""
    width = max(1, int(rate * bin_s))
    usable = len(samples) - len(samples) % width
    if usable <= 0:
        return np.zeros(0, dtype=np.float32)
    power = (samples[:usable].reshape(-1, width) ** 2).mean(axis=1)
    return np.asarray(power, dtype=np.float32)


def cut_offset(
    tracks: Sequence[tuple[np.ndarray, int]],
    target_s: float,
    search_s: float = SEARCH_S,
    bin_s: float = BIN_S,
    quiet_s: float = QUIET_S,
) -> float:
    """Where to end a chunk of a meeting: the quietest short moment near the
    length asked for, measured across every side at once so a cut lands where
    nobody is talking rather than where one side happens to pause.

    This is the whole difference between chunking being free and chunking being
    expensive. Measured over 25 minutes of speech, chunks cut on the minute
    regardless of what was being said cost 160% more decoding time than the
    same audio transcribed whole — the transcriber spends it on fragments of
    words it cannot resolve — while chunks cut at a pause cost 13% more, and at
    two minutes each were cheaper than the whole file.

    Falls back to the length asked for when there is nothing to measure, which
    is what happens on a stretch too short to hold a search."""
    if not tracks:
        return target_s
    bins: list[np.ndarray] = [loudness(s, rate, bin_s) for s, rate in tracks]
    width = max(1, int(quiet_s / bin_s))
    span = min((len(b) for b in bins if len(b)), default=0)
    if span < width:
        return target_s
    total = np.sum([b[:span] for b in bins], axis=0)
    # the mean power of every window this long, as one pass over a running sum
    running = np.concatenate([[0.0], np.cumsum(total, dtype=np.float64)])
    windows = running[width:] - running[:-width]
    first = max(0, int((target_s - search_s) / bin_s))
    last = min(len(windows), int((target_s + search_s) / bin_s) + 1)
    if last <= first:
        return target_s
    at = first + int(np.argmin(windows[first:last]))
    return (at + width / 2) * bin_s


def shifted(segments: Sequence[Segment], offset_ms: int) -> list[Segment]:
    """A chunk's lines placed in the meeting they came out of. The transcriber
    is handed one stretch at a time and times every line from the start of the
    stretch, so without this every chunk of a meeting would claim to have
    happened in its first minutes."""
    return [
        replace(s, t0_ms=s.t0_ms + offset_ms, t1_ms=s.t1_ms + offset_ms) for s in segments
    ]


def prompt_tail(segments: Sequence[Segment], words: int = 30) -> str:
    """The last words of what has been transcribed so far, to hand the next
    chunk as its opening context. Whisper primes each of its own 30-second
    windows with the text before it, and cutting a meeting into chunks throws
    that away at every seam; this is what puts it back."""
    spoken = " ".join(s.text.strip() for s in segments).split()
    return " ".join(spoken[-words:]) if spoken else ""
