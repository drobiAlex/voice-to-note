from dataclasses import dataclass
from typing import NotRequired, TypedDict

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
    """The notes an LLM produces for one memo. Every section a reader is shown
    is required: a backend that omits one has not done the job. The project the
    memo belongs to is the exception — it is a suggestion for filing rather than
    something the notes display, so a backend that has nothing to say about it,
    or a note template written before it was asked for, still produces usable
    notes."""

    title: str
    project: NotRequired[str]
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
class TrackFormat:
    """How one recorded track is laid out on disk. The recorder writes each
    side in whatever its own device offered — nothing converts anything until
    the two sides are mixed — so every reader of a track has to be told this
    rather than assume it."""

    rate: int
    channels: int
    bits: int
    is_float: bool

    @property
    def frame_bytes(self) -> int:
        """One sample across every channel, which is the smallest amount of a
        track anybody may cut on: half a frame handed on shifts every channel
        after it by a sample and turns speech into noise."""
        return self.channels * self.bits // 8


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
    # nothing has changed the memo since it was stored, which is not the same as
    # it having been changed at the moment it was made
    updated_at: str | None = None
    # when the recording itself was made, as its container said — how a file
    # offered a second time is recognised as one already here. Empty for a memo
    # stored before this was kept, and for a source that never said
    recorded_at: str | None = None
    # when somebody put this memo away: hidden from the listings, still there to
    # open. Empty is the live memo, and nothing here is ever a deletion
    archived_at: str | None = None
    # where this memo's words came from when no recording on this machine did:
    # the canonical video URL, which is how a video offered a second time is
    # recognised as one already imported. Empty for a memo made from audio
    source_url: str | None = None


@dataclass(frozen=True)
class MemoListing:
    """One memo as a list of them shows it: the memo itself, plus the few facts
    about its contents a row carries beside the name. They travel with the memo
    because a list is drawn all at once, and asking after each row's voices and
    repairs separately would cost a query per memo on screen."""

    memo: Memo
    speakers: int
    refined: bool
    edited: bool
    open_todos: int


@dataclass(frozen=True)
class Todo:
    """One thing a memo committed somebody to, tracked past the extraction that
    found it so that checking it off means something. The project is the memo's
    own rather than the to-do's: a memo filed elsewhere takes everything it
    committed to along with it."""

    id: int
    memo_id: int
    text: str
    owner: str
    deadline: str
    status: str
    project: str


@dataclass(frozen=True)
class Extraction:
    """The structured notes an LLM produced for a memo."""

    backend: str
    data: NotesPayload
    created_at: str


@dataclass(frozen=True)
class Message:
    """One turn in a conversation about memos: who said it, and — for the
    model's turns — which backend was answering as. The backend is kept per
    message rather than per conversation because the chain can move a thread
    from one backend to the next mid-way, and a reader wondering why the tone
    changed deserves the answer."""

    role: str
    text: str
    created_at: str
    backend: str = ""
    id: int | None = None


@dataclass(frozen=True)
class Conversation:
    """A thread about one or several memos, kept so it can be reopened. The
    memos are the thread's scope: what every question in it is answered
    from. A memo deleted since drops out of the scope but not out of the
    thread's history, which is why the titles travel beside the ids."""

    id: int
    title: str
    created_at: str
    updated_at: str | None
    memo_ids: tuple[int, ...] = ()
    memo_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationListing:
    """One conversation as a list of them shows it, the counts travelling
    with it for the same reason MemoListing's do: a list is drawn all at
    once, and a query per row for its length would be a query per row on
    screen."""

    conversation: Conversation
    messages: int
    last_at: str
