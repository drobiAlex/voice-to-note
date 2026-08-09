from dataclasses import dataclass
from typing import TypedDict

import numpy as np


class ActionItem(TypedDict):
    """Something a speaker committed to doing, with whoever owns it."""

    task: str
    owner: str | None
    deadline: str | None


class DateMention(TypedDict):
    """A date or time that came up, and what it was about."""

    date: str
    context: str


class NotesPayload(TypedDict):
    """The notes an LLM produces for one memo. Every section is required: the
    reader shows them all, so a backend that omits one has not done the job."""

    title: str
    summary: str
    action_items: list[ActionItem]
    decisions: list[str]
    key_insights: list[str]
    open_questions: list[str]
    dates: list[DateMention]
    tags: list[str]


@dataclass(frozen=True)
class Segment:
    """One timed line of transcript — what the transcriber produces."""

    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None
    id: int | None = None
    # what a repair pass made of this line; the raw text above is never replaced
    refined_text: str | None = None


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
    # free text, not an enum: the sidebar lists whatever projects exist
    project: str = "other"


@dataclass(frozen=True)
class Extraction:
    """The structured notes an LLM produced for a memo."""

    backend: str
    data: NotesPayload
    created_at: str
