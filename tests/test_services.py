import json

import numpy as np
import pytest

from voice_to_note import config, services
from voice_to_note.domain import Segment, Speaker, Turn
from voice_to_note.gateways import llm, whisper
from voice_to_note.storage.repository import Repository

ALICE_VOICE = np.array([1.0, 0.0, 0.0], dtype=np.float32)
BOB_VOICE = np.array([0.0, 1.0, 0.0], dtype=np.float32)

NOTES = {
    "title": "Sprint sync",
    "summary": "We discussed the release.",
    "action_items": [],
    "decisions": [],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": [],
}


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "services.db")
    yield r
    r.close()


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "memo.wav"
    path.write_bytes(b"RIFF")
    return path


def add_memo(repo, wav, *, segments=(), speakers=(), filename="memo.m4a") -> int:
    return repo.create_memo(
        filename=filename,
        wav_path=str(wav),
        duration_s=1.0,
        language="en",
        segments=list(segments),
        speakers=list(speakers),
    )


def fake_diarization(monkeypatch, turns, embeddings) -> None:
    monkeypatch.setattr(services.sherpa, "diarize", lambda _wav: list(turns))
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: dict(embeddings))


def fake_llm(monkeypatch, *, claude=None, ollama=None) -> list[str]:
    """Wire both backends; a value of None means "not installed". Returns prompts seen."""
    seen: list[str] = []

    def backend(reply):
        def call(prompt, schema=None):
            seen.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return call

    monkeypatch.setattr(services.llm, "claude_available", lambda: claude is not None)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: ollama is not None)
    monkeypatch.setattr(services.llm, "claude_complete", backend(claude))
    monkeypatch.setattr(services.llm, "ollama_complete", backend(ollama))
    return seen


def test_processing_gives_the_transcriber_the_audio_duration(repo, tmp_path, monkeypatch):
    # whisper's timeout budget is derived from the duration, so it has to arrive
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(services.audio, "to_wav16k", lambda _src, _dst: None)
    monkeypatch.setattr(services.audio, "duration_seconds", lambda _path: 137.5)
    seen = {}

    def transcribe(wav_path, duration_s):
        seen["wav"], seen["duration_s"] = wav_path, duration_s
        return {
            "transcription": [{"text": " Hello ", "offsets": {"from": 0, "to": 1000}}],
            "result": {"language": "en"},
        }

    monkeypatch.setattr(services.whisper, "transcribe", transcribe)
    fake_diarization(monkeypatch, [Turn(0, 1000, "S1")], {"S1": ALICE_VOICE})

    result = services.process_memo(repo, src)

    assert seen["duration_s"] == 137.5
    assert seen["wav"].name.startswith("standup-")
    assert repo.memo(result.memo_id).duration_s == 137.5
    assert [s.text for s in repo.segments(result.memo_id)] == ["Hello"]


def test_transcription_timeout_scales_with_duration_above_a_floor():
    assert whisper.timeout_for(10) == whisper.TIMEOUT_FLOOR_S
    assert whisper.timeout_for(600) == 2400


def test_rediarization_ignores_the_memos_own_speakers_when_matching(repo, wav, monkeypatch):
    memo_id = add_memo(
        repo,
        wav,
        segments=[Segment(0, 1000, "Hello", speaker="S1")],
        speakers=[Speaker("S1", "Alice", ALICE_VOICE)],
    )
    add_memo(repo, wav, speakers=[Speaker("S1", "Bob", BOB_VOICE)], filename="other.m4a")
    fake_diarization(monkeypatch, [Turn(0, 1000, "S2")], {"S2": ALICE_VOICE})

    assert services.rediarize(repo, memo_id) == ["S2"]
    assert [s.speaker for s in repo.segments(memo_id)] == ["S2"]
    # matching the memo against its own stored voice would name this Alice
    assert repo.display_names(memo_id) == {"S2": "S2"}


def test_a_named_speaker_keeps_its_name_through_rediarization(repo, wav, monkeypatch):
    memo_id = add_memo(
        repo,
        wav,
        segments=[Segment(0, 1000, "Hello", speaker="S1")],
        speakers=[Speaker("S1", "Alice", ALICE_VOICE)],
    )
    add_memo(repo, wav, speakers=[Speaker("S1", "Bob", BOB_VOICE)], filename="other.m4a")
    fake_diarization(monkeypatch, [Turn(0, 1000, "S1")], {"S1": BOB_VOICE})

    log: list[str] = []
    services.rediarize(repo, memo_id, log=log.append)

    assert repo.display_names(memo_id) == {"S1": "Alice"}
    assert not [line for line in log if "sounds like" in line]


def test_an_unnamed_speaker_is_auto_named_from_another_memo(repo, wav, monkeypatch):
    memo_id = add_memo(
        repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")], speakers=[Speaker("S1")]
    )
    add_memo(repo, wav, speakers=[Speaker("S1", "Bob", BOB_VOICE)], filename="other.m4a")
    fake_diarization(monkeypatch, [Turn(0, 1000, "S1")], {"S1": BOB_VOICE})

    log: list[str] = []
    services.rediarize(repo, memo_id, log=log.append)

    assert repo.display_names(memo_id) == {"S1": "Bob"}
    assert log[-1] == "  S1 sounds like Bob (similarity 1.00) — auto-named"


def test_unparseable_claude_output_falls_back_to_ollama(repo, wav, monkeypatch):
    memo_id = add_memo(
        repo,
        wav,
        segments=[Segment(0, 1000, "Ship it", speaker="S1")],
        speakers=[Speaker("S1", "Alice")],
    )
    prompts = fake_llm(monkeypatch, claude="I'm not sure what you mean.", ollama=json.dumps(NOTES))

    assert services.run_extraction(repo, memo_id) == f"ollama/{config.OLLAMA_MODEL}"
    assert repo.extraction(repo.memo(memo_id).id).data["title"] == "Sprint sync"
    assert "[00:00] Alice: Ship it" in prompts[0]


def test_notes_json_round_trips_the_stored_extraction(repo, wav):
    # the scripting contract: `vtn notes --json` parses back to what was stored
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    repo.save_extraction(memo_id, "claude", NOTES)
    assert json.loads(services.notes_json(repo, memo_id)) == NOTES


def test_extraction_error_names_every_backend_that_failed(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    fake_llm(
        monkeypatch,
        claude=llm.BackendError("claude -p failed: boom"),
        ollama="still not JSON",
    )

    with pytest.raises(services.ExtractionError) as err:
        services.run_extraction(repo, memo_id)

    message = str(err.value)
    assert "claude: claude -p failed: boom" in message
    assert "ollama: " in message
    assert repo.extraction(memo_id) is None
    assert repo.memo(memo_id).status == "transcribed"


def test_extraction_without_a_backend_says_how_to_get_one(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    fake_llm(monkeypatch)

    with pytest.raises(services.ExtractionError, match="install claude CLI"):
        services.run_extraction(repo, memo_id)


def test_ask_returns_the_first_working_backend(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    prompts = fake_llm(monkeypatch, claude="  They ship on Friday.  ")

    assert services.ask(repo, memo_id, "When do they ship?") == (
        "claude",
        "They ship on Friday.",
    )
    assert "When do they ship?" in prompts[0]
    assert "[00:00] S1: Ship it" in prompts[0]


def test_ask_about_a_missing_memo_is_rejected_before_calling_a_backend(repo, monkeypatch):
    prompts = fake_llm(monkeypatch, claude="anything")
    with pytest.raises(services.NotFound):
        services.ask(repo, 999, "anything?")
    assert prompts == []
