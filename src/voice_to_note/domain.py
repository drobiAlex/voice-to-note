from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Segment:
    """One timed line of transcript — what the transcriber produces."""

    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Turn:
    """A stretch of audio one person speaks — what the diarizer produces."""

    start_ms: int
    end_ms: int
    speaker: str


@dataclass(frozen=True, eq=False)
class Speaker:
    """A voice in one memo: its label, any name given, and its fingerprint."""

    label: str
    name: str | None = None
    embedding: np.ndarray | None = None


@dataclass(frozen=True)
class SpeakerMatch:
    """A voice recognised as someone already named in an earlier memo."""

    name: str
    similarity: float


@dataclass(frozen=True)
class Memo:
    """One recording the app has processed."""

    id: int
    filename: str
    wav_path: str
    duration_s: float | None
    language: str | None
    status: str
    created_at: str


@dataclass(frozen=True)
class Extraction:
    """The structured notes an LLM produced for a memo."""

    backend: str
    data: dict
    created_at: str
