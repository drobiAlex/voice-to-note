import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from . import config
from .domain import Extraction, Memo, Speaker, SpeakerMatch, Turn
from .gateways import audio, llm, sherpa, whisper
from .storage.repository import Repository
from .transforms.notes import SCHEMA, parse_notes, render_notes
from .transforms.segments import (
    segments_as_dicts,
    segments_from_whisper,
    transcript_text,
)
from .transforms.speakers import (
    assign_speakers,
    auto_named,
    match_known_speakers,
    resolve_speaker_names,
)

Log = Callable[[str], None]
T = TypeVar("T")


class NotFound(Exception):
    """A memo, speaker or extraction the caller asked for does not exist."""


class ExtractionError(Exception):
    """No LLM backend produced a usable result."""


@dataclass
class ProcessResult:
    """What `vtn process` reports back once a memo is safely stored."""

    memo_id: int
    segment_count: int
    labels: list[str]
    language: str


def _silent(message: str) -> None:
    """Default for callers that want the work done without progress reports."""
    pass


def require_memo(repo: Repository, memo_id: int) -> Memo:
    """Finds a memo, or fails with something the user can act on."""
    memo = repo.memo(memo_id)
    if not memo:
        raise NotFound(f"no memo with id {memo_id}")
    return memo


def process_memo(repo: Repository, src: Path, log: Log = _silent) -> ProcessResult:
    """The whole pipeline for a new recording: audio in, stored memo out."""
    wav = config.UPLOADS_DIR / f"{src.stem}-{uuid.uuid4().hex[:8]}.wav"
    log(f"converting {src.name} …")
    audio.to_wav16k(src, wav)
    duration = audio.duration_seconds(wav)
    log(f"transcribing ({duration:.0f}s audio) …")
    raw = whisper.transcribe(wav, duration)
    segs = segments_from_whisper(raw)
    language = raw.get("result", {}).get("language", "")
    log("diarizing …")
    turns = sherpa.diarize(wav)
    segs = assign_speakers(segs, turns)
    labels, speakers, matches = _identify(repo, wav, turns, keep_names={})
    memo_id = repo.create_memo(
        filename=src.name,
        wav_path=str(wav),
        duration_s=duration,
        language=language,
        segments=segs,
        speakers=speakers,
    )
    _report_matches(log, matches, keep_names={})
    return ProcessResult(memo_id, len(segs), labels, language)


def rediarize(repo: Repository, memo_id: int, log: Log = _silent) -> list[str]:
    """Runs speaker detection over a memo again, keeping the names people gave."""
    memo = require_memo(repo, memo_id)
    wav = Path(memo.wav_path)
    if not wav.exists():
        raise NotFound(f"wav missing: {wav}")
    keep_names = repo.named_speakers(memo_id)
    log(f"diarizing memo {memo_id} …")
    turns = sherpa.diarize(wav)
    segs = assign_speakers(repo.segments(memo_id), turns)
    labels, speakers, matches = _identify(repo, wav, turns, keep_names, exclude=memo_id)
    repo.save_diarization(memo_id, segs, speakers)
    _report_matches(log, matches, keep_names)
    return labels


def _identify(
    repo: Repository,
    wav: Path,
    turns: list[Turn],
    keep_names: dict[str, str],
    exclude: int | None = None,
) -> tuple[list[str], list[Speaker], dict[str, SpeakerMatch]]:
    """Works out who each voice belongs to, reusing names from earlier memos."""
    embeddings = sherpa.speaker_embeddings(wav, turns)
    pool = repo.known_embeddings(exclude_memo_id=exclude)
    matches = match_known_speakers(embeddings, pool, config.MATCH_THRESHOLD)
    labels = sorted({t.speaker for t in turns})
    return labels, resolve_speaker_names(labels, embeddings, matches, keep_names), matches


def _report_matches(
    log: Log, matches: dict[str, SpeakerMatch], keep_names: dict[str, str]
) -> None:
    """Tells the user which speakers were named by voice rather than by hand."""
    for label, m in auto_named(matches, keep_names):
        log(f"  {label} sounds like {m.name} (similarity {m.similarity:.2f}) — auto-named")


def transcript(repo: Repository, memo_id: int) -> str:
    """The speaker-labeled transcript a person or an LLM reads."""
    return transcript_text(repo.segments(memo_id), repo.display_names(memo_id))


def _complete(
    prompt: str,
    schema: dict | None,
    parse: Callable[[str], T],
    failure: str,
    unavailable: str,
) -> tuple[str, T]:
    """Gets an answer from the best backend that gives a usable one; a reply
    that cannot be used demotes that backend rather than failing outright."""
    errors = []
    if llm.claude_available():
        try:
            return "claude", parse(llm.claude_complete(prompt))
        except (llm.BackendError, ValueError) as e:
            errors.append(f"claude: {e}")
    if llm.ollama_available():
        try:
            return f"ollama/{config.OLLAMA_MODEL}", parse(llm.ollama_complete(prompt, schema))
        except (llm.BackendError, ValueError) as e:
            errors.append(f"ollama: {e}")
    if errors:
        raise ExtractionError(f"{failure}: " + "; ".join(errors))
    raise ExtractionError(
        f"{unavailable}: install claude CLI, or run Ollama and"
        f" `ollama pull {config.OLLAMA_MODEL}`"
    )


def run_extraction(repo: Repository, memo_id: int) -> str:
    """Turns a memo into structured notes and files them against it."""
    backend, data = _complete(
        llm.notes_prompt(transcript(repo, memo_id)),
        schema=SCHEMA,
        parse=parse_notes,
        failure="extraction failed",
        unavailable="no extraction backend",
    )
    repo.save_extraction(memo_id, backend, data)
    return backend


def _extraction(repo: Repository, memo_id: int) -> Extraction:
    """Fetches a memo's notes, telling the user how to create them if missing."""
    extraction = repo.extraction(memo_id)
    if not extraction:
        raise NotFound(f"no extraction for memo {memo_id} — run: vtn extract {memo_id}")
    return extraction


def notes(repo: Repository, memo_id: int) -> str:
    """The notes as a person reads them."""
    return render_notes(_extraction(repo, memo_id))


def notes_json(repo: Repository, memo_id: int) -> str:
    """The notes as a script reads them."""
    return json.dumps(_extraction(repo, memo_id).data, ensure_ascii=False)


def transcript_json(repo: Repository, memo_id: int) -> str:
    """The transcript as a script reads it, speakers already named."""
    segments = segments_as_dicts(repo.segments(memo_id), repo.display_names(memo_id))
    return json.dumps(segments, ensure_ascii=False)


def memos_json(repo: Repository) -> str:
    """The memo list as a script reads it."""
    return json.dumps([asdict(m) for m in repo.memos()], ensure_ascii=False)


def ask(repo: Repository, memo_id: int, question: str) -> tuple[str, str]:
    """Answers a question about one memo, grounded only in what was said."""
    require_memo(repo, memo_id)
    return _complete(
        llm.ask_prompt(transcript(repo, memo_id), question),
        schema=None,
        parse=str.strip,
        failure="ask failed",
        unavailable="no backend",
    )


def rename_speaker(repo: Repository, memo_id: int, label: str, name: str) -> None:
    """Puts a real name on a speaker so later memos recognise that voice."""
    if not repo.rename_speaker(memo_id, label, name):
        raise NotFound(f"no speaker {label} in memo {memo_id}")
