from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Segment:
    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Turn:
    start_ms: int
    end_ms: int
    speaker: str


@dataclass(frozen=True, eq=False)
class Speaker:
    label: str
    name: str | None = None
    embedding: np.ndarray | None = None


@dataclass(frozen=True)
class SpeakerMatch:
    name: str
    similarity: float


@dataclass(frozen=True)
class Memo:
    id: int
    filename: str
    wav_path: str
    duration_s: float | None
    language: str | None
    status: str
    created_at: str


@dataclass(frozen=True)
class Extraction:
    backend: str
    data: dict
    created_at: str
