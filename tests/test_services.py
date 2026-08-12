import json
import random
import re
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from voice_to_note import config, services
from voice_to_note.domain import Segment, Speaker, Turn
from voice_to_note.gateways import llm, whisper
from voice_to_note.transforms import refine

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
    monkeypatch.setattr(services.sherpa, "diarize", lambda _wav, num_speakers=None: list(turns))
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: dict(embeddings))


def fake_llm(monkeypatch, *, claude=None, ollama=None) -> list[str]:
    """Wire both backends; a value of None means "not installed". Returns prompts seen."""
    seen: list[str] = []

    def backend(reply):
        def call(prompt, schema=None, model=None):
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


def fake_backend(name, *, available=True, reply=None) -> llm.Backend:
    """A backend whose availability and reply are fixed for the test, standing
    in for the registry entries _backends() would otherwise look up by name."""
    return llm.Backend(
        name=name,
        available=lambda: available,
        complete=lambda prompt, schema, model: (
            (_ for _ in ()).throw(reply) if isinstance(reply, Exception) else reply
        ),
        describe=lambda model: name,
    )


# --- choosing which backends to try -----------------------------------------


def test_backends_are_tried_in_the_order_the_setting_names_them(repo, wav, monkeypatch):
    monkeypatch.setattr(
        services.llm,
        "BACKENDS",
        {"first": fake_backend("first"), "second": fake_backend("second")},
    )
    monkeypatch.setattr(services.config, "LLM_BACKENDS", "second,first")

    assert [b.name for b in services._backends()] == ["second", "first"]


def test_a_failing_backend_falls_through_to_the_next_one_in_order(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    monkeypatch.setattr(
        services.llm,
        "BACKENDS",
        {
            "first": fake_backend("first", reply=llm.BackendError("boom")),
            "second": fake_backend("second", reply=json.dumps(NOTES)),
        },
    )
    monkeypatch.setattr(services.config, "LLM_BACKENDS", "first,second")

    backend = services.run_extraction(repo, memo_id)

    assert backend == "second"


def test_an_unavailable_backend_is_skipped_without_being_called(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    monkeypatch.setattr(
        services.llm,
        "BACKENDS",
        {
            "first": fake_backend("first", available=False),
            "second": fake_backend("second", reply=json.dumps(NOTES)),
        },
    )
    monkeypatch.setattr(services.config, "LLM_BACKENDS", "first,second")

    backend = services.run_extraction(repo, memo_id)

    assert backend == "second"


def test_an_unknown_backend_name_in_the_setting_is_rejected(monkeypatch):
    monkeypatch.setattr(services.config, "LLM_BACKENDS", "claude,not-a-real-backend")

    with pytest.raises(services.InvalidInput, match="not-a-real-backend"):
        services._backends()


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


def test_the_pipeline_says_which_stage_it_is_on_before_that_stage_runs(
    repo, tmp_path, monkeypatch
):
    # a screen counting the stages off would be one behind all the way through
    # if each stage only reported itself once its minutes of work were over
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    seen: list = []
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(services.audio, "to_wav16k", lambda _src, _dst: seen.append("converted"))
    monkeypatch.setattr(services.audio, "duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        services.whisper,
        "transcribe",
        lambda _wav, _duration: seen.append("transcribed")
        or {"transcription": [], "result": {"language": "en"}},
    )
    monkeypatch.setattr(
        services.sherpa, "diarize", lambda _wav, num_speakers=None: seen.append("diarized") or []
    )
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: {})

    services.process_memo(repo, src, progress=lambda stage, doing: seen.append((stage, doing)))

    assert seen == [
        (1, "converting"),
        "converted",
        (2, "transcribing"),
        "transcribed",
        (3, "diarizing"),
        "diarized",
    ]


def test_a_pipeline_nobody_is_watching_runs_the_same_way(repo, tmp_path, monkeypatch):
    # the command line has nowhere to draw a stage bar and passes no callback
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(services.audio, "to_wav16k", lambda _src, _dst: None)
    monkeypatch.setattr(services.audio, "duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        services.whisper,
        "transcribe",
        lambda _wav, _duration: {
            "transcription": [{"text": " Hello ", "offsets": {"from": 0, "to": 1000}}],
            "result": {"language": "en"},
        },
    )
    fake_diarization(monkeypatch, [Turn(0, 1000, "S1")], {"S1": ALICE_VOICE})

    result = services.process_memo(repo, src)

    assert repo.memo(result.memo_id).filename == "standup.m4a"


def _process_setup(tmp_path, monkeypatch) -> Path:
    """Wires every stage of process_memo up to diarization, so a test can
    focus on what diarize=False or a pinned speaker count changes without
    repeating the conversion and transcription plumbing."""
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(services.audio, "to_wav16k", lambda _src, _dst: None)
    monkeypatch.setattr(services.audio, "duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        services.whisper,
        "transcribe",
        lambda _wav, _duration: {
            "transcription": [{"text": " Hello ", "offsets": {"from": 0, "to": 1000}}],
            "result": {"language": "en"},
        },
    )
    return src


def test_skipping_speaker_detection_never_reaches_sherpa(repo, tmp_path, monkeypatch):
    # a memo imported with just its transcript has nothing for sherpa to do;
    # calling it at all would be minutes of work nobody asked for
    src = _process_setup(tmp_path, monkeypatch)

    def refused(*_a, **_k):
        raise AssertionError("sherpa.diarize must not run when diarize=False")

    monkeypatch.setattr(services.sherpa, "diarize", refused)
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", refused)

    result = services.process_memo(repo, src, diarize=False)

    assert result.labels == []
    assert repo.display_names(result.memo_id) == {}


def test_a_pinned_speaker_count_reaches_diarization(repo, tmp_path, monkeypatch):
    src = _process_setup(tmp_path, monkeypatch)
    seen: dict = {}

    def diarize(_wav, num_speakers=None):
        seen["num_speakers"] = num_speakers
        return [Turn(0, 1000, "S1")]

    monkeypatch.setattr(services.sherpa, "diarize", diarize)
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: {})

    services.process_memo(repo, src, num_speakers=2)

    assert seen["num_speakers"] == 2


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


def test_rediarization_forwards_a_pinned_speaker_count_to_sherpa(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    seen: dict = {}

    def diarize(_wav, num_speakers=None):
        seen["num_speakers"] = num_speakers
        return [Turn(0, 1000, "S1")]

    monkeypatch.setattr(services.sherpa, "diarize", diarize)
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: {})

    services.rediarize(repo, memo_id, num_speakers=3)

    assert seen["num_speakers"] == 3


def test_rediarization_without_a_speaker_count_leaves_it_to_sherpa(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    seen: dict = {}

    def diarize(_wav, num_speakers=None):
        seen["num_speakers"] = num_speakers
        return [Turn(0, 1000, "S1")]

    monkeypatch.setattr(services.sherpa, "diarize", diarize)
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: {})

    services.rediarize(repo, memo_id)

    assert seen["num_speakers"] is None


@pytest.mark.parametrize("bad_count", [0, -3])
def test_rediarization_refuses_a_speaker_count_below_one(repo, bad_count):
    with pytest.raises(services.InvalidInput, match="speaker"):
        services.rediarize(repo, 999, num_speakers=bad_count)


@pytest.mark.parametrize(
    "typed, expected", [("", None), ("auto", None), ("AUTO ", None), ("2", 2)]
)
def test_speaker_count_parses_what_was_typed(typed, expected):
    assert services.speaker_count(typed) == expected


@pytest.mark.parametrize("typed", ["0", "x"])
def test_speaker_count_refuses_what_it_cannot_use(typed):
    with pytest.raises(services.InvalidInput):
        services.speaker_count(typed)


def test_process_steps_parses_the_default():
    assert services.process_steps("speakers,notes") == frozenset({"speakers", "notes"})


@pytest.mark.parametrize("typed", ["", "   "])
def test_process_steps_of_blank_text_is_a_transcript_only_import(typed):
    assert services.process_steps(typed) == frozenset()


def test_process_steps_refuses_an_unknown_token():
    with pytest.raises(services.InvalidInput, match="bogus"):
        services.process_steps("speakers,bogus")


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


def test_extraction_leaves_claude_to_pick_its_own_model(repo, wav, monkeypatch):
    # extraction is the harder task and stays on the configured model; only
    # repair is cheap enough to move to a smaller one
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    seen: dict = {}

    def claude(prompt, model=None):
        seen["model"] = model
        return json.dumps(NOTES)

    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "claude_complete", claude)

    services.run_extraction(repo, memo_id)

    assert seen["model"] is None


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


def test_extraction_with_a_custom_template_sends_its_text_in_the_prompt(
    repo, wav, monkeypatch, templates_dir
):
    templates_dir.mkdir()
    (templates_dir / "standup.md").write_text("Summarize this standup:\n")
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    prompts = fake_llm(monkeypatch, claude=json.dumps(NOTES))

    services.run_extraction(repo, memo_id, template="standup")

    assert "Summarize this standup:" in prompts[0]


def test_extraction_refuses_an_unknown_template_before_any_backend_is_called(
    repo, wav, monkeypatch, templates_dir
):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    prompts = fake_llm(monkeypatch, claude=json.dumps(NOTES))

    with pytest.raises(services.InvalidInput, match="unknown note template"):
        services.run_extraction(repo, memo_id, template="bogus")

    assert prompts == []


def test_ask_returns_the_first_working_backend(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    prompts = fake_llm(monkeypatch, claude="  They ship on Friday.  ")

    assert services.ask(repo, memo_id, "When do they ship?") == (
        "claude",
        "They ship on Friday.",
    )
    assert "When do they ship?" in prompts[0]
    assert "[00:00] S1: Ship it" in prompts[0]


@pytest.mark.parametrize("question", ["", "   "])
def test_asking_nothing_at_all_is_refused_before_a_backend_is_called(
    repo, wav, question, monkeypatch
):
    # an empty question still costs a model call and comes back with whatever the
    # model makes of being asked nothing
    memo_id = add_memo(repo, wav)
    prompts = fake_llm(monkeypatch, claude="anything")

    with pytest.raises(services.InvalidInput):
        services.ask(repo, memo_id, question)

    assert prompts == []


def test_a_question_is_tidied_before_it_is_put_to_a_backend(repo, wav, monkeypatch):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    prompts = fake_llm(monkeypatch, claude="Friday.")

    services.ask(repo, memo_id, "  When do they ship?  ")

    assert "  When do they ship?  " not in prompts[0]
    assert "When do they ship?" in prompts[0]


def test_a_question_can_be_tidied_for_a_screen_that_asks_first(repo):
    # the screen refuses a blank question while its modal is still open, which
    # means asking the same question the store would ask, in the same words
    assert services.question("  When do they ship?  ") == "When do they ship?"

    with pytest.raises(services.InvalidInput):
        services.question("   ")


def test_ask_about_a_missing_memo_is_rejected_before_calling_a_backend(repo, monkeypatch):
    prompts = fake_llm(monkeypatch, claude="anything")
    with pytest.raises(services.NotFound):
        services.ask(repo, 999, "anything?")
    assert prompts == []


def refine_reply(pairs: dict[int, str]) -> str:
    """A backend answering a repair request in the shape the parser demands."""
    return json.dumps({"segments": [{"id": i, "text": t} for i, t in pairs.items()]})


def add_transcript(repo, wav, lines: list[str]) -> list[int]:
    """Stores a memo of raw transcript lines and hands back their ids."""
    memo_id = add_memo(repo, wav, segments=[Segment(i * 1000, i * 1000 + 900, t, speaker="S1")
                                            for i, t in enumerate(lines)])
    return [memo_id, *[s.id for s in repo.segments(memo_id)]]


def refine_targets(prompt: str) -> dict[int, str]:
    """The lines one window asked to have repaired, by id. The context lines
    around them are marked read-only, so a stub can answer that window with
    exactly the ids it was asked for and no others."""
    asked = re.findall(r"^\[(\d+)\] (?!\(read-only\))[^:]*: (.*)$", prompt, re.MULTILINE)
    return {int(seg_id): text for seg_id, text in asked}


def long_transcript(repo, wav, windows: int) -> int:
    """A memo long enough to fill the given number of repair windows."""
    lines = [f"line {i} as it was heard" for i in range(refine.CHUNK_SIZE * windows)]
    memo_id, *_ids = add_transcript(repo, wav, lines)
    return memo_id


def test_refining_a_memo_stores_the_repairs_beside_the_original(repo, wav, monkeypatch):
    memo_id, first, second = add_transcript(
        repo, wav, ["so their going to ship on friday", "yeah we agreed on that"]
    )
    fake_llm(monkeypatch, claude=refine_reply({
        first: "So they're going to ship on Friday.",
        second: "Yeah, we agreed on that.",
    }))

    result = services.refine_transcript(repo, memo_id)

    stored = repo.segments(memo_id)
    assert [s.refined_text for s in stored] == [
        "So they're going to ship on Friday.",
        "Yeah, we agreed on that.",
    ]
    assert [s.text for s in stored] == [
        "so their going to ship on friday",
        "yeah we agreed on that",
    ]
    assert len(result.changes) == 2
    assert result.flagged == []


def test_a_line_the_model_rewrote_is_flagged_and_left_as_recorded(repo, wav, monkeypatch):
    memo_id, only = add_transcript(repo, wav, ["we ship the release on friday"])
    fake_llm(monkeypatch, claude=refine_reply({only: "The cat sat upon the mat in silence."}))

    result = services.refine_transcript(repo, memo_id)

    assert repo.segments(memo_id)[0].refined_text is None
    assert result.flagged == [only]
    assert result.changes == []


def test_a_repair_identical_to_the_original_counts_as_untouched(repo, wav, monkeypatch):
    memo_id, only = add_transcript(repo, wav, ["nothing wrong with this line"])
    fake_llm(monkeypatch, claude=refine_reply({only: "nothing wrong with this line"}))

    result = services.refine_transcript(repo, memo_id)

    assert result.untouched == 1
    assert result.changes == []


def test_a_line_a_window_left_out_of_its_reply_is_flagged_not_lost(repo, wav, monkeypatch):
    # a weaker repair model can leave lines out of its reply; the pass must
    # still complete, with the omitted line surfaced instead of silently kept
    memo_id, first, second = add_transcript(
        repo, wav, ["so their going to ship on friday", "yeah we agreed on that"]
    )
    fake_llm(monkeypatch, claude=refine_reply({first: "So they're going to ship on Friday."}))

    result = services.refine_transcript(repo, memo_id)

    stored = repo.segments(memo_id)
    assert [s.text for s in stored] == [
        "so their going to ship on friday",
        "yeah we agreed on that",
    ]
    assert stored[1].refined_text is None
    assert result.flagged == [second]
    assert len(result.changes) == 1
    assert result.untouched == 0


def test_a_dry_run_shows_the_repairs_without_storing_any(repo, wav, monkeypatch):
    memo_id, only = add_transcript(repo, wav, ["so their going to ship on friday"])
    fake_llm(monkeypatch, claude=refine_reply({only: "So they're going to ship on Friday."}))

    result = services.refine_transcript(repo, memo_id, dry_run=True)

    assert repo.segments(memo_id)[0].refined_text is None
    assert [(c.segment_id, c.before, c.after) for c in result.changes] == [
        (only, "so their going to ship on friday", "So they're going to ship on Friday.")
    ]


def test_refining_a_missing_memo_is_rejected_before_calling_a_backend(repo, monkeypatch):
    prompts = fake_llm(monkeypatch, claude="anything")

    with pytest.raises(services.NotFound):
        services.refine_transcript(repo, 999)

    assert prompts == []


def test_refining_without_a_backend_says_how_to_get_one(repo, wav, monkeypatch):
    memo_id, _only = add_transcript(repo, wav, ["so their going to ship on friday"])
    fake_llm(monkeypatch)

    with pytest.raises(services.ExtractionError, match="install claude CLI"):
        services.refine_transcript(repo, memo_id)


def test_the_local_backend_is_held_to_the_reply_shape(repo, wav, monkeypatch):
    # constrained decoding is the only thing keeping a small model on-format
    memo_id, only = add_transcript(repo, wav, ["so their going to ship on friday"])
    seen: dict = {}

    def ollama(prompt, schema=None):
        seen["schema"] = schema
        return refine_reply({only: "So they're going to ship on Friday."})

    monkeypatch.setattr(services.llm, "claude_available", lambda: False)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: True)
    monkeypatch.setattr(services.llm, "ollama_complete", ollama)

    services.refine_transcript(repo, memo_id)

    assert seen["schema"] == refine.REFINE_SCHEMA


def test_refining_runs_on_the_cheaper_refine_model(repo, wav, monkeypatch):
    # repair is typo-level work done once per window of a memo, so it is worth
    # paying for on a smaller model than the one that reads the whole transcript
    memo_id, only = add_transcript(repo, wav, ["so their going to ship on friday"])
    seen: dict = {}

    def claude(prompt, model=None):
        seen["model"] = model
        return refine_reply({only: "So they're going to ship on Friday."})

    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "claude_complete", claude)

    services.refine_transcript(repo, memo_id)

    assert seen["model"] == config.REFINE_MODEL


def test_refine_window_setting_controls_the_chunk_size(repo, wav, monkeypatch):
    memo_id, only = add_transcript(repo, wav, ["so their going to ship on friday"])
    fake_llm(monkeypatch, claude=refine_reply({only: "So they're going to ship on Friday."}))
    monkeypatch.setattr(services.config, "REFINE_WINDOW", 7)
    seen: dict = {}

    def spy(segments, chunk_size=refine.CHUNK_SIZE):
        seen["chunk_size"] = chunk_size
        return refine.chunk_segments(segments, chunk_size=chunk_size)

    monkeypatch.setattr(services, "chunk_segments", spy)

    services.refine_transcript(repo, memo_id)

    assert seen["chunk_size"] == 7


def test_repair_windows_are_in_flight_at_the_same_time(repo, wav, monkeypatch):
    # one backend round trip per window, taken in turn, is minutes of waiting on
    # a long memo — the wall clock is what makes the feature usable at all
    memo_id = long_transcript(repo, wav, windows=services.REFINE_WORKERS)
    together = threading.Barrier(services.REFINE_WORKERS, timeout=5)

    def claude(prompt, model=None):
        # windows taken one after another never gather here, and the wait breaks
        together.wait()
        return refine_reply({i: t.capitalize() for i, t in refine_targets(prompt).items()})

    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: False)
    monkeypatch.setattr(services.llm, "claude_complete", claude)

    result = services.refine_transcript(repo, memo_id)

    assert len(result.changes) == refine.CHUNK_SIZE * services.REFINE_WORKERS


def test_a_repair_lands_on_its_own_line_however_the_windows_finish(repo, wav, monkeypatch):
    # windows come back in whatever order the backend answers them; a transcript
    # wearing another window's words would be worse than one never repaired
    memo_id = long_transcript(repo, wav, windows=services.REFINE_WORKERS)
    jitter = random.Random(7)

    def claude(prompt, model=None):
        targets = refine_targets(prompt)
        time.sleep(jitter.uniform(0, 0.05))
        # every repair says which window it came from, so a misfiled one shows
        return refine_reply({i: f"{t} w{min(targets)}" for i, t in targets.items()})

    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: False)
    monkeypatch.setattr(services.llm, "claude_complete", claude)

    services.refine_transcript(repo, memo_id)

    stored = repo.segments(memo_id)
    expected = []
    for start in range(0, len(stored), refine.CHUNK_SIZE):
        window = stored[start : start + refine.CHUNK_SIZE]
        expected += [f"{s.text} w{window[0].id}" for s in window]
    assert [s.refined_text for s in repo.segments(memo_id)] == expected


def test_one_window_failing_fails_the_whole_pass(repo, wav, monkeypatch):
    # a transcript half repaired is the one outcome nobody asked for: the caller
    # hears the failure, and what is stored is still what was transcribed
    memo_id = long_transcript(repo, wav, windows=2)
    doomed = repo.segments(memo_id)[refine.CHUNK_SIZE].id

    def claude(prompt, model=None):
        targets = refine_targets(prompt)
        if doomed in targets:
            raise llm.BackendError("claude fell over")
        return refine_reply({i: t.capitalize() for i, t in targets.items()})

    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: False)
    monkeypatch.setattr(services.llm, "claude_complete", claude)

    with pytest.raises(services.ExtractionError, match="refinement failed"):
        services.refine_transcript(repo, memo_id)

    assert all(s.refined_text is None for s in repo.segments(memo_id))


def test_the_diff_reads_as_one_before_and_after_per_line():
    result = services.RefineResult(
        changes=[services.Change(3, "so their going", "So they're going")],
        flagged=[],
        untouched=0,
    )

    assert services.refine_diff_text(result) == "[3] so their going\n      → So they're going"


def add_refined(repo, wav, lines: list[tuple[str, str | None]]) -> int:
    """A memo where some lines have been repaired and some have not."""
    memo_id = add_memo(
        repo,
        wav,
        segments=[
            Segment(i * 1000, i * 1000 + 900, raw, speaker="S1")
            for i, (raw, _refined) in enumerate(lines)
        ],
    )
    stored = repo.segments(memo_id)
    repo.update_refinements(
        memo_id,
        {s.id: refined for s, (_raw, refined) in zip(stored, lines, strict=True) if refined},
    )
    return memo_id


def test_a_repaired_line_is_what_a_reader_is_shown(repo, wav):
    memo_id = add_refined(
        repo, wav, [("so their going to ship", "So they're going to ship."), ("yeah agreed", None)]
    )

    assert services.transcript_lines(repo, memo_id) == (
        "00:00  S1: So they're going to ship.\n00:01  S1: yeah agreed"
    )


def test_the_transcription_as_heard_is_still_available(repo, wav):
    memo_id = add_refined(repo, wav, [("so their going to ship", "So they're going to ship.")])

    assert services.transcript_lines(repo, memo_id, raw=True) == "00:00  S1: so their going to ship"


def test_extraction_is_given_the_repaired_words(repo, wav, monkeypatch):
    # refine then extract is the whole point of the feature: better words in,
    # better notes out
    memo_id = add_refined(repo, wav, [("we ship on friyay", "We ship on Friday.")])
    prompts = fake_llm(monkeypatch, claude=json.dumps(NOTES))

    services.run_extraction(repo, memo_id)

    assert "We ship on Friday." in prompts[0]
    assert "friyay" not in prompts[0]


def test_a_question_is_answered_against_the_repaired_words(repo, wav, monkeypatch):
    memo_id = add_refined(repo, wav, [("we ship on friyay", "We ship on Friday.")])
    prompts = fake_llm(monkeypatch, claude="Friday.")

    services.ask(repo, memo_id, "When do they ship?")

    assert "We ship on Friday." in prompts[0]


def test_the_json_transcript_carries_the_repairs(repo, wav):
    memo_id = add_refined(repo, wav, [("so their going", "So they're going.")])

    assert json.loads(services.transcript_json(repo, memo_id))[0]["text"] == "So they're going."


def test_the_json_transcript_can_be_asked_for_the_original(repo, wav):
    memo_id = add_refined(repo, wav, [("so their going", "So they're going.")])

    text = json.loads(services.transcript_json(repo, memo_id, raw=True))[0]["text"]
    assert text == "so their going"


def test_a_memo_nobody_refined_reads_exactly_as_it_did_before(repo, wav):
    # the regression net: refinement must be invisible until someone runs it
    memo_id = add_refined(repo, wav, [("first line", None), ("second line", None)])

    assert services.transcript_lines(repo, memo_id) == (
        "00:00  S1: first line\n00:01  S1: second line"
    )
    assert services.transcript(repo, memo_id) == "[00:00] S1: first line second line"
    assert [d["text"] for d in json.loads(services.transcript_json(repo, memo_id))] == [
        "first line",
        "second line",
    ]


def add_project_memo(repo, wav, filename: str, project: str) -> int:
    """A stored memo filed under one project."""
    return repo.create_memo(
        filename=filename, wav_path=str(wav), duration_s=1.0,
        language="en", segments=[], project=project,
    )


def test_the_listing_can_be_narrowed_to_one_project(repo, wav):
    add_project_memo(repo, wav, "work.m4a", "work")
    add_project_memo(repo, wav, "home.m4a", "personal")

    listing = services.memos_text(repo, project="work")

    assert "work.m4a" in listing
    assert "home.m4a" not in listing


def test_the_json_listing_can_be_narrowed_too(repo, wav):
    add_project_memo(repo, wav, "work.m4a", "work")
    add_project_memo(repo, wav, "home.m4a", "personal")

    listed = json.loads(services.memos_json(repo, project="work"))

    assert [m["filename"] for m in listed] == ["work.m4a"]


def test_the_json_listing_says_which_project_a_memo_is_in(repo, wav):
    add_project_memo(repo, wav, "work.m4a", "work")

    assert json.loads(services.memos_json(repo))[0]["project"] == "work"


def test_moving_a_memo_files_it_under_the_new_project(repo, wav):
    memo_id = add_project_memo(repo, wav, "work.m4a", "work")

    services.move_memo(repo, memo_id, "side")

    assert repo.memo(memo_id).project == "side"


def test_moving_a_memo_that_was_never_stored_is_refused(repo):
    with pytest.raises(services.NotFound):
        services.move_memo(repo, 999, "side")


@pytest.mark.parametrize("project", ["", "   "])
def test_moving_a_memo_into_a_nameless_project_is_refused(repo, wav, project):
    # a blank name would show as an empty column and an empty sidebar row
    memo_id = add_project_memo(repo, wav, "work.m4a", "work")

    with pytest.raises(services.InvalidInput):
        services.move_memo(repo, memo_id, project)

    assert repo.memo(memo_id).project == "work"


def test_a_project_name_is_tidied_before_it_is_stored(repo, wav):
    memo_id = add_project_memo(repo, wav, "work.m4a", "other")

    services.move_memo(repo, memo_id, "  work  ")

    assert repo.memo(memo_id).project == "work"


def test_processing_into_a_nameless_project_is_refused_before_any_work(repo, tmp_path, monkeypatch):
    # the recording is converted and transcribed before it is stored; a name we
    # will refuse at the end must be refused at the start
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    converted: list = []
    monkeypatch.setattr(services.audio, "to_wav16k", lambda *a: converted.append(a))

    with pytest.raises(services.InvalidInput):
        services.process_memo(repo, src, project="   ")

    assert converted == []


def test_the_project_list_is_available_to_a_screen(repo, wav):
    # the sidebar needs names and counts without reaching past services
    add_project_memo(repo, wav, "work.m4a", "work")
    add_project_memo(repo, wav, "home.m4a", "personal")

    assert services.projects(repo) == [("personal", 1), ("work", 1)]


def test_renaming_a_project_carries_every_memo_in_it(repo, wav):
    add_project_memo(repo, wav, "a.m4a", "work")
    add_project_memo(repo, wav, "b.m4a", "work")
    add_project_memo(repo, wav, "c.m4a", "personal")

    moved = services.rename_project(repo, "work", "client")

    assert moved == 2
    assert services.projects(repo) == [("client", 2), ("personal", 1)]


def test_renaming_a_project_nobody_has_used_is_refused(repo, wav):
    add_project_memo(repo, wav, "a.m4a", "work")

    with pytest.raises(services.NotFound, match="no project ghost"):
        services.rename_project(repo, "ghost", "client")


@pytest.mark.parametrize("name", ["", "   "])
def test_renaming_a_project_to_nothing_is_refused(repo, wav, name):
    add_project_memo(repo, wav, "a.m4a", "work")

    with pytest.raises(services.InvalidInput):
        services.rename_project(repo, "work", name)

    assert services.projects(repo) == [("work", 1)]


def test_renaming_a_project_onto_one_that_exists_merges_them(repo, wav):
    # projects have no table of their own, they are just a value on a memo, so a
    # name already in use is not a clash to refuse but two piles becoming one
    add_project_memo(repo, wav, "a.m4a", "work")
    add_project_memo(repo, wav, "b.m4a", "personal")

    moved = services.rename_project(repo, "work", "personal")

    assert moved == 1
    assert services.projects(repo) == [("personal", 2)]


def test_a_project_renamed_with_stray_spacing_is_tidied_like_any_other(repo, wav):
    add_project_memo(repo, wav, "a.m4a", "work")

    services.rename_project(repo, "work", "  client  ")

    assert services.projects(repo) == [("client", 1)]


def test_removing_a_project_files_its_memos_under_other(repo, wav):
    add_project_memo(repo, wav, "a.m4a", "work")
    add_project_memo(repo, wav, "b.m4a", "work")

    moved = services.remove_project(repo, "work")

    assert moved == 2
    assert services.projects(repo) == [("other", 2)]


def test_removing_the_other_project_is_refused(repo, wav):
    # it is where removing a project puts the memos, so it has nowhere to go
    add_project_memo(repo, wav, "a.m4a", "other")

    with pytest.raises(services.InvalidInput):
        services.remove_project(repo, "other")

    assert services.projects(repo) == [("other", 1)]


def test_removing_a_project_nobody_has_used_is_refused(repo):
    with pytest.raises(services.NotFound, match="no project ghost"):
        services.remove_project(repo, "ghost")


def test_work_off_the_main_thread_gets_its_own_connection(repo, wav):
    # sqlite refuses a connection used from a thread that did not make it, so a
    # background job cannot borrow the screen's
    memo_id = add_memo(repo, wav)
    failure: list[str] = []

    def in_another_thread() -> None:
        try:
            with services.open_repo(repo) as own:
                services.move_memo(own, memo_id, "work")
        except Exception as e:  # noqa: BLE001 - the test is what it reports
            failure.append(f"{type(e).__name__}: {e}")

    worker = threading.Thread(target=in_another_thread)
    worker.start()
    worker.join()

    assert failure == []
    assert repo.memo(memo_id).project == "work"


@pytest.mark.parametrize("tag", ["", "   "])
def test_searching_for_a_tag_of_nothing_is_refused(repo, tag):
    # a blank tag matches nothing, which would read as a search that ran and
    # honestly found nothing rather than one that was never a search
    with pytest.raises(services.InvalidInput):
        services.memos(repo, tag=tag)


def test_a_tag_is_tidied_before_it_is_searched_for(repo, wav):
    memo_id = add_memo(repo, wav)
    repo.save_extraction(memo_id, "claude", {"title": "A", "tags": ["release"]})

    assert [m.id for m in services.memos(repo, tag="  release  ")] == [memo_id]


def test_the_printed_listing_can_be_narrowed_to_one_tag(repo, wav):
    tagged = add_memo(repo, wav, filename="tagged.m4a")
    repo.save_extraction(tagged, "claude", {"title": "A", "tags": ["release"]})
    add_memo(repo, wav, filename="plain.m4a")

    listing = services.memos_text(repo, tag="release")

    assert "tagged.m4a" in listing
    assert "plain.m4a" not in listing


def test_the_info_for_a_memo_gathers_what_a_reader_would_ask(repo, wav):
    memo_id = repo.create_memo(
        filename="standup.m4a", wav_path=str(wav), duration_s=75.0, language="en",
        segments=[Segment(0, 1000, "we ship friday", speaker="S1")],
        speakers=[Speaker("S1")], project="work",
    )

    info = services.memo_info(repo, memo_id)

    assert (info.filename, info.project, info.duration) == ("standup.m4a", "work", "1m 15s")
    assert (info.language, info.status) == ("en", "transcribed")
    assert (info.speakers, info.refined, info.edited) == (1, False, False)


def test_a_memo_nobody_has_changed_reads_as_updated_when_it_was_made(repo, wav):
    # a screen has to print something, and "never" would be a lie about a memo
    # that has been sitting there correct since the day it arrived
    memo_id = add_memo(repo, wav)

    info = services.memo_info(repo, memo_id)

    assert info.updated == info.created


def test_the_info_notices_a_repaired_transcript_and_an_edited_note(repo, wav):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "helo")])
    (stored,) = repo.segments(memo_id)
    repo.update_refinements(memo_id, {stored.id: "hello"})
    repo.save_notes_md(memo_id, "# My own words")

    info = services.memo_info(repo, memo_id)

    assert (info.refined, info.edited) == (True, True)


def test_asking_after_a_memo_that_was_never_stored_is_refused(repo):
    with pytest.raises(services.NotFound):
        services.memo_info(repo, 999)


def test_the_info_reads_as_labels_and_values_lined_up(repo, wav):
    memo_id = add_memo(repo, wav, filename="standup.m4a")

    text = services.memo_info_text(repo, memo_id)

    assert "file:     standup.m4a" in text
    assert "project:  other" in text
    assert "repaired: no" in text


def test_memos_can_be_had_as_records_rather_than_a_printed_table(repo, wav):
    # a screen builds its own rows; it should not have to parse memos_text
    add_project_memo(repo, wav, "work.m4a", "work")
    add_project_memo(repo, wav, "home.m4a", "personal")

    listed = services.memos(repo, project="work")

    assert [m.filename for m in listed] == ["work.m4a"]
    assert listed[0].project == "work"


# --- the memo list as a table lays it out ---------------------------------


def test_the_memo_table_puts_a_memos_state_in_columns_beside_its_name(repo, wav):
    memo_id = repo.create_memo(
        filename="standup.m4a", wav_path=str(wav), duration_s=75.0, language="en",
        segments=[Segment(0, 1000, "we ship friday", speaker="S1")],
        speakers=[Speaker("S1"), Speaker("S2")], project="work",
    )

    (row,) = services.memo_rows(repo)

    assert services.MEMO_COLUMNS == (
        "name", "duration", "speakers", "status", "created", "updated",
    )
    assert row.id == memo_id
    assert (row.name, row.duration, row.speakers) == ("standup.m4a", "1m 15s", "2")
    assert row.status == "transcribed"
    assert row.created != ""
    assert row.updated == row.created


def test_a_repaired_transcript_and_a_hand_written_note_are_marked_in_the_status(repo, wav):
    # the two the info modal calls repaired and edited, folded into the one
    # column a table has room for
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "helo")])
    (stored,) = repo.segments(memo_id)
    repo.update_refinements(memo_id, {stored.id: "hello"})
    repo.save_notes_md(memo_id, "# My own words")

    (row,) = services.memo_rows(repo)

    assert row.status == "transcribed (repaired, edited)"


def test_a_memo_nobody_has_repaired_or_rewritten_carries_no_marks(repo, wav):
    add_memo(repo, wav, segments=[Segment(0, 1000, "hello")])

    (row,) = services.memo_rows(repo)

    assert row.status == "transcribed"


def test_the_table_says_the_same_about_a_memo_as_its_info_does(repo, wav):
    # two readings of one memo: a memo repaired in the modal is repaired in the
    # list, or the screen contradicts itself depending on where you look
    memo_id = add_memo(
        repo, wav, segments=[Segment(0, 1000, "helo")], speakers=[Speaker("S1")]
    )
    (stored,) = repo.segments(memo_id)
    repo.update_refinements(memo_id, {stored.id: "hello"})

    (row,) = services.memo_rows(repo)
    info = services.memo_info(repo, memo_id)

    assert (row.name, row.duration) == (info.filename, info.duration)
    assert (row.created, row.updated) == (info.created, info.updated)
    assert row.speakers == str(info.speakers)
    assert row.status.startswith(info.status)
    assert ("repaired" in row.status, "edited" in row.status) == (info.refined, info.edited)


def test_the_table_is_filled_by_one_query_however_many_memos_it_lists(repo, wav):
    # the state beside each name would otherwise cost a query per row, paid
    # again on every reload
    for i in range(4):
        add_memo(repo, wav, filename=f"memo{i}.m4a", speakers=[Speaker("S1")])
    statements: list[str] = []
    repo.con.set_trace_callback(statements.append)

    rows = services.memo_rows(repo)

    repo.con.set_trace_callback(None)
    assert len(rows) == 4
    assert len(statements) == 1


def test_the_table_can_be_narrowed_to_one_project_or_one_tag(repo, wav):
    work = add_project_memo(repo, wav, "work.m4a", "work")
    repo.save_extraction(work, "claude", {"title": "A", "tags": ["release"]})
    add_project_memo(repo, wav, "home.m4a", "personal")

    assert [r.name for r in services.memo_rows(repo, project="work")] == ["work.m4a"]
    assert [r.name for r in services.memo_rows(repo, tag="  release  ")] == ["work.m4a"]


@pytest.mark.parametrize("tag", ["", "   "])
def test_a_table_asked_for_a_tag_of_nothing_is_refused(repo, tag):
    with pytest.raises(services.InvalidInput):
        services.memo_rows(repo, tag=tag)


def test_notes_come_as_markdown_for_a_reader_that_renders_it(repo, wav):
    # the action items are the section the plain renderer loses to Markdown, so
    # they are the section worth asserting on
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])
    committed = NOTES | {
        "action_items": [{"task": "Cut the release", "owner": "Alice", "deadline": "Friday"}]
    }
    repo.save_extraction(memo_id, "claude", committed)

    markdown = services.notes_markdown(repo, memo_id)

    assert markdown.startswith("# Sprint sync")
    assert "## Action items" in markdown
    assert "- [ ] Cut the release — Alice, Friday" in markdown


def test_a_memo_with_no_notes_yields_markdown_that_says_so(repo, wav):
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Hello", speaker="S1")])

    assert "no notes" in services.notes_markdown(repo, memo_id).lower()


def extracted_memo(repo, wav) -> int:
    """A memo the model has already written notes for."""
    memo_id = add_memo(repo, wav, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    repo.save_extraction(memo_id, "claude", NOTES)
    return memo_id


def test_an_edited_note_is_what_a_reader_is_shown(repo, wav):
    memo_id = extracted_memo(repo, wav)

    services.save_notes(repo, memo_id, "# In my own words\n\nWhat actually mattered.")

    assert services.notes_markdown(repo, memo_id).startswith("# In my own words")


def test_a_memo_nobody_edited_still_reads_from_the_extraction(repo, wav):
    # the regression net: editing must be invisible until someone edits
    memo_id = extracted_memo(repo, wav)

    assert services.notes_markdown(repo, memo_id).startswith("# Sprint sync")


def test_the_printed_notes_stay_the_models_own_words(repo, wav):
    # `vtn notes` prints the extraction; the edit is the on-screen note
    memo_id = extracted_memo(repo, wav)
    services.save_notes(repo, memo_id, "# In my own words")

    assert services.notes(repo, memo_id).startswith("# Sprint sync")
    assert json.loads(services.notes_json(repo, memo_id)) == NOTES


def test_an_empty_note_is_refused_rather_than_stored(repo, wav):
    memo_id = extracted_memo(repo, wav)

    with pytest.raises(services.InvalidInput):
        services.save_notes(repo, memo_id, "   \n  ")


def test_a_note_is_tidied_at_the_edges_before_it_is_stored(repo, wav):
    memo_id = extracted_memo(repo, wav)

    services.save_notes(repo, memo_id, "\n\n# In my own words\n\n")

    assert services.notes_markdown(repo, memo_id) == "# In my own words"


def test_saving_a_note_on_a_memo_that_does_not_exist_is_refused(repo):
    with pytest.raises(services.NotFound):
        services.save_notes(repo, 999, "# Anything")


def test_re_extracting_over_an_edited_note_is_refused(repo, wav, monkeypatch):
    # hours of somebody's editing must not vanish because they retyped a command
    memo_id = extracted_memo(repo, wav)
    services.save_notes(repo, memo_id, "# In my own words")
    prompts = fake_llm(monkeypatch, claude=json.dumps(NOTES))

    with pytest.raises(services.InvalidInput, match="--force"):
        services.run_extraction(repo, memo_id)

    assert prompts == []
    assert services.notes_markdown(repo, memo_id) == "# In my own words"


def test_re_extracting_on_purpose_replaces_the_edited_note(repo, wav, monkeypatch):
    memo_id = extracted_memo(repo, wav)
    services.save_notes(repo, memo_id, "# In my own words")
    fake_llm(monkeypatch, claude=json.dumps(NOTES | {"title": "Freshly extracted"}))

    services.run_extraction(repo, memo_id, force=True)

    assert services.notes_markdown(repo, memo_id).startswith("# Freshly extracted")


def test_a_forced_re_extraction_clears_the_edit_in_the_same_breath(repo, wav, monkeypatch):
    # a separate clearing step could fail on its own and leave the edit masking
    # notes it was never written over
    memo_id = extracted_memo(repo, wav)
    services.save_notes(repo, memo_id, "# In my own words")
    fake_llm(monkeypatch, claude=json.dumps(NOTES | {"title": "Freshly extracted"}))
    separate_clears: list = []
    monkeypatch.setattr(
        type(repo), "save_notes_md", lambda _self, *args: separate_clears.append(args)
    )

    services.run_extraction(repo, memo_id, force=True)

    assert separate_clears == []
    assert repo.notes_md(memo_id) is None


def test_a_memos_speakers_can_be_listed_for_a_screen_to_offer(repo, wav):
    # a screen offering "rename who?" needs the labels and what they are called
    memo_id = add_memo(
        repo,
        wav,
        segments=[Segment(0, 1000, "Hi", speaker="S1")],
        speakers=[Speaker("S1", "Alice"), Speaker("S2")],
    )

    assert services.speakers(repo, memo_id) == {"S1": "Alice", "S2": "S2"}


def named_memo(repo, wav) -> int:
    """A memo with one voice in it, still going by its label."""
    return add_memo(
        repo,
        wav,
        segments=[Segment(0, 1000, "Hi", speaker="S1")],
        speakers=[Speaker("S1")],
    )


@pytest.mark.parametrize("name", ["", "   "])
def test_naming_a_speaker_nothing_is_refused(repo, wav, name):
    # a nameless speaker reads as Unknown everywhere and cannot be searched for
    memo_id = named_memo(repo, wav)

    with pytest.raises(services.InvalidInput):
        services.rename_speaker(repo, memo_id, "S1", name)

    assert services.speakers(repo, memo_id) == {"S1": "S1"}


def test_a_speaker_name_is_tidied_before_it_is_stored(repo, wav):
    memo_id = named_memo(repo, wav)

    services.rename_speaker(repo, memo_id, "S1", "  Alice  ")

    assert services.speakers(repo, memo_id) == {"S1": "Alice"}


# --- writing settings -------------------------------------------------------


@pytest.fixture
def config_path(monkeypatch, tmp_path):
    """Points config_set/unset/reset at a scratch vtn.toml instead of the
    real one, with no VTN_ environment override left over from the machine
    running the test to confuse a test that did not ask for one."""
    path = tmp_path / "vtn.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    for key in config.SETTINGS:
        monkeypatch.delenv(f"VTN_{key.upper()}", raising=False)
    return path


def test_setting_a_value_writes_it_to_the_config_file(config_path):
    services.config_set("whisper_model", "small")

    assert config.read_config_file(config_path) == {"whisper_model": "small"}


def test_setting_a_value_reports_it_and_that_it_waits_for_a_restart(config_path):
    message = services.config_set("num_speakers", "3")

    assert message == "num_speakers set to 3 (takes effect on next start)"


def test_setting_preserves_other_keys_already_in_the_file(config_path):
    config_path.write_text('num_speakers = 2\n')

    services.config_set("whisper_model", "small")

    assert config.read_config_file(config_path) == {
        "num_speakers": 2,
        "whisper_model": "small",
    }


def test_setting_an_unknown_key_is_refused(config_path):
    with pytest.raises(services.InvalidInput, match="unknown setting: bogus"):
        services.config_set("bogus", "1")


def test_setting_a_value_that_does_not_cast_is_refused(config_path):
    with pytest.raises(services.InvalidInput, match="not a valid num_speakers: nope"):
        services.config_set("num_speakers", "nope")


def test_setting_a_value_warns_when_an_env_override_still_wins(config_path, monkeypatch):
    monkeypatch.setenv("VTN_WHISPER_MODEL", "tiny")

    message = services.config_set("whisper_model", "small")

    assert "VTN_WHISPER_MODEL" in message
    assert "still wins" in message


# --- prompt templates --------------------------------------------------------


@pytest.fixture
def templates_dir(monkeypatch, tmp_path):
    """Points template reads and writes at a scratch directory instead of the
    real one, so a test never touches a saved override on the machine
    running it."""
    path = tmp_path / "templates"
    monkeypatch.setattr(config, "TEMPLATES_DIR", path)
    return path


def test_every_registered_template_starts_out_built_in(templates_dir):
    assert services.template_rows() == [(name, "built-in") for name in llm.TEMPLATES]


def test_an_override_file_marks_its_template_overridden(templates_dir):
    templates_dir.mkdir()
    (templates_dir / "notes.md").write_text("custom")

    rows = dict(services.template_rows())

    assert rows["notes"] == "overridden"
    assert rows["refine"] == "built-in"


def test_template_text_returns_the_built_in_text_absent_an_override(templates_dir):
    assert services.template_text("notes") == llm.TEMPLATES["notes"]


def test_template_text_returns_a_saved_override(templates_dir):
    templates_dir.mkdir()
    (templates_dir / "ask").with_suffix(".md").write_text("custom ask prompt")

    assert services.template_text("ask") == "custom ask prompt"


def test_an_unknown_template_name_is_refused(templates_dir):
    with pytest.raises(services.InvalidInput, match="unknown template: bogus"):
        services.template_text("bogus")


def test_resetting_an_overridden_template_deletes_its_file(templates_dir):
    templates_dir.mkdir()
    override = templates_dir / "notes.md"
    override.write_text("custom")

    message = services.template_reset("notes")

    assert not override.exists()
    assert "restored" in message


def test_resetting_a_template_already_built_in_reports_so_without_erroring(templates_dir):
    message = services.template_reset("refine")

    assert "already built-in" in message


# --- custom note templates ---------------------------------------------------


def test_note_templates_is_just_notes_when_the_directory_is_empty_or_missing(templates_dir):
    assert services.note_templates() == ["notes"]


def test_note_templates_includes_a_custom_file_dropped_beside_the_others(templates_dir):
    templates_dir.mkdir()
    (templates_dir / "standup.md").write_text("Summarize the standup")

    assert services.note_templates() == ["notes", "standup"]


def test_note_templates_excludes_overrides_of_the_non_note_builtins(templates_dir):
    # refine.md and ask.md shadow other prompts, not this one — they must not
    # show up as a note template a user could pick to extract with
    templates_dir.mkdir()
    (templates_dir / "refine.md").write_text("override")
    (templates_dir / "ask.md").write_text("override")

    assert services.note_templates() == ["notes"]


def test_template_text_reads_a_custom_note_template_file_directly(templates_dir):
    templates_dir.mkdir()
    (templates_dir / "standup.md").write_text("Summarize the standup")

    assert services.template_text("standup") == "Summarize the standup"


def test_resetting_a_custom_note_template_explains_deleting_the_file_without_touching_it(
    templates_dir,
):
    templates_dir.mkdir()
    custom = templates_dir / "standup.md"
    custom.write_text("Summarize the standup")

    message = services.template_reset("standup")

    assert custom.exists()
    assert "standup" in message
    assert "custom template" in message


def test_unsetting_a_value_removes_it_from_the_file(config_path):
    config_path.write_text('whisper_model = "small"\nnum_speakers = 2\n')

    services.config_unset("whisper_model")

    assert config.read_config_file(config_path) == {"num_speakers": 2}


def test_unsetting_a_value_never_written_says_it_was_already_default(config_path):
    assert services.config_unset("whisper_model") == "whisper_model is already default"


def test_unsetting_an_unknown_key_is_refused(config_path):
    with pytest.raises(services.InvalidInput, match="unknown setting: bogus"):
        services.config_unset("bogus")


def test_reset_clears_every_registered_setting(config_path):
    config_path.write_text('whisper_model = "small"\nnum_speakers = 2\n')

    services.config_reset()

    assert config.read_config_file(config_path) == {}


def test_reset_keeps_keys_it_does_not_own(config_path):
    config_path.write_text('whisper_model = "small"\nnot_a_setting = 42\n')

    services.config_reset()

    assert config.read_config_file(config_path) == {"not_a_setting": 42}


# --- installing whisper.cpp and its models --------------------------------


def configured_paths(monkeypatch, tmp_path):
    """Points every setup path at a scratch directory, mirroring config.py's layout."""
    vendor = tmp_path / "vendor" / "whisper.cpp"
    models = tmp_path / "models"
    monkeypatch.setattr(services.config, "VENDOR", vendor)
    monkeypatch.setattr(services.config, "WHISPER_BIN", vendor / "build" / "bin" / "whisper-cli")
    monkeypatch.setattr(services.config, "MODELS_DIR", models)
    monkeypatch.setattr(services.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "data" / "uploads")
    monkeypatch.setattr(services.config, "WHISPER_MODEL", "large-v3-turbo")
    monkeypatch.setattr(services.config, "WHISPER_MODEL_PATH", models / "ggml-large-v3-turbo.bin")
    monkeypatch.setattr(services.config, "VAD_MODEL_PATH", models / "ggml-silero-v5.1.2.bin")
    monkeypatch.setattr(
        services.config,
        "SEG_MODEL_PATH",
        models / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx",
    )
    monkeypatch.setattr(services.config, "EMB_MODEL_PATH", models / "nemo_en_titanet_large.onnx")
    monkeypatch.setattr(services.config, "CAPTURE_BIN", tmp_path / "bin" / "vtn-capture")
    app = tmp_path / "bin" / "VTN Recorder.app"
    monkeypatch.setattr(services.config, "MENUBAR_APP", app)
    monkeypatch.setattr(services.config, "MENUBAR_BIN", app / "Contents" / "MacOS" / "vtn-menubar")
    return vendor


def _write_binary(path: Path) -> None:
    """Puts a runnable stand-in where setup looks for a compiled artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("bin")
    path.chmod(0o755)


def _write_native_helpers() -> None:
    """Puts both macOS-only builds — the capture helper and the menu bar
    recorder — where setup looks for them."""
    _write_binary(services.config.CAPTURE_BIN)
    _write_binary(services.config.MENUBAR_BIN)


def stub_bootstrap(monkeypatch, *, vad_fails=False, cloned_urls: list | None = None) -> list:
    """Fakes every outside-world step setup takes, recording the order they ran in.
    cloned_urls, if given, also collects the repo url each clone call received."""
    calls: list = []

    def clone(vendor, repo_url):
        calls.append("clone")
        if cloned_urls is not None:
            cloned_urls.append(repo_url)
        vendor.mkdir(parents=True)

    def build(vendor):
        calls.append("build")
        binary = vendor / "build" / "bin" / "whisper-cli"
        binary.parent.mkdir(parents=True)
        binary.write_text("bin")
        binary.chmod(0o755)

    def download(vendor, script, args):
        calls.append(script)
        if script == "download-vad-model.sh" and vad_fails:
            raise services.bootstrap.GatewayError("network unreachable")

    def fetch_tar_bz2(url, dst, tick=lambda read, total: None):
        calls.append("seg")

    def fetch(url, dst, tick=lambda read, total: None):
        calls.append("emb")

    def build_capture(source, plist, dst):
        calls.append("capture")
        _write_binary(dst)

    def build_menubar(source, plist, app):
        calls.append("menubar")
        _write_binary(app / "Contents" / "MacOS" / "vtn-menubar")

    # setup's last two steps only run on a Mac, so the platform is pinned rather
    # than left to whichever machine the suite happens to run on
    monkeypatch.setattr(services.sys, "platform", "darwin")
    monkeypatch.setattr(services.bootstrap, "require_tools", lambda: calls.append("tools"))
    monkeypatch.setattr(services.bootstrap, "clone_whisper", clone)
    monkeypatch.setattr(services.bootstrap, "build_whisper", build)
    monkeypatch.setattr(services.bootstrap, "build_capture", build_capture)
    monkeypatch.setattr(services.bootstrap, "build_menubar", build_menubar)
    monkeypatch.setattr(services.bootstrap, "download_model_script", download)
    monkeypatch.setattr(services.bootstrap, "fetch_tar_bz2", fetch_tar_bz2)
    monkeypatch.setattr(services.bootstrap, "fetch", fetch)
    return calls


def test_setup_runs_every_step_when_nothing_is_on_disk(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    calls = stub_bootstrap(monkeypatch)

    result = services.setup()

    assert result == "setup complete"
    assert calls == [
        "tools", "clone", "build",
        "download-ggml-model.sh", "download-vad-model.sh", "seg", "emb", "capture", "menubar",
    ]
    assert services.config.MODELS_DIR.is_dir()
    assert services.config.DATA_DIR.is_dir()
    assert services.config.UPLOADS_DIR.is_dir()


def test_setup_clones_whisper_from_the_configured_repo_url(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(services.config, "WHISPER_REPO_URL", "https://example.com/mirror.git")
    cloned_urls: list = []
    stub_bootstrap(monkeypatch, cloned_urls=cloned_urls)

    services.setup()

    assert cloned_urls == ["https://example.com/mirror.git"]


def test_setup_numbers_and_times_each_step_it_actually_runs(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    stub_bootstrap(monkeypatch)
    logged: list = []
    clock = iter(range(0, 100, 3))

    services.setup(log=logged.append, now=lambda: next(clock))

    assert logged[0] == "[1/8] cloning whisper.cpp …"
    assert logged[1] == "      done in 3s"
    assert logged[2] == "[2/8] building whisper.cpp (Metal — takes a few minutes) …"
    assert logged[3] == "      done in 3s"
    assert logged[4] == f"[3/8] downloading whisper model {config.WHISPER_MODEL} …"
    assert logged[5] == "      done in 3s"
    assert logged[6] == "[4/8] downloading VAD model …"
    assert logged[7] == "      done in 3s"
    assert logged[8] == "[5/8] downloading speaker segmentation model (~6 MB) …"
    assert logged[9] == "      done in 3s"
    assert logged[10] == "[6/8] downloading speaker embedding model (~97 MB) …"
    assert logged[11] == "      done in 3s"
    assert logged[12] == "[7/8] building meeting-capture helper …"
    assert logged[13] == "      done in 3s"
    assert logged[14] == "[8/8] building menu bar recorder …"
    assert logged[15] == "      done in 3s"


def test_setup_reports_a_skipped_step_without_running_or_timing_it(monkeypatch, tmp_path):
    vendor = configured_paths(monkeypatch, tmp_path)
    vendor.mkdir(parents=True)
    binary = services.config.WHISPER_BIN
    binary.parent.mkdir(parents=True)
    binary.write_text("bin")
    binary.chmod(0o755)
    services.config.MODELS_DIR.mkdir(parents=True)
    services.config.WHISPER_MODEL_PATH.write_text("model")
    services.config.VAD_MODEL_PATH.write_text("vad")
    services.config.SEG_MODEL_PATH.parent.mkdir(parents=True)
    services.config.SEG_MODEL_PATH.write_text("seg")
    (services.config.MODELS_DIR / "nemo_en_titanet_large.onnx").write_text("emb")
    _write_native_helpers()
    stub_bootstrap(monkeypatch)
    logged: list = []

    services.setup(log=logged.append)

    assert logged == [
        "[1/8] whisper.cpp source — already installed",
        "[2/8] whisper.cpp build — already installed",
        "[3/8] whisper model — already installed",
        "[4/8] VAD model — already installed",
        "[5/8] speaker segmentation model — already installed",
        "[6/8] speaker embedding model — already installed",
        "[7/8] meeting-capture helper — already installed",
        "[8/8] menu bar recorder — already installed",
    ]


def test_setup_reports_download_progress_through_its_own_callback(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    stub_bootstrap(monkeypatch)

    def fetch(url, dst, tick=lambda read, total: None):
        tick(42_000_000, 97_000_000)

    monkeypatch.setattr(services.bootstrap, "fetch", fetch)
    lines: list = []

    services.setup(download=lines.append)

    assert any("MB" in line and "%" in line for line in lines)


def test_download_line_shows_size_and_percent_once_total_is_known():
    line = services._download_line("nemo_en_titanet_large.onnx", 42_000_000, 97_000_000)

    assert line == "  nemo_en_titanet_large.onnx 42.0/97.0 MB (43%)"


def test_download_line_omits_the_total_when_the_server_did_not_send_one():
    assert services._download_line("model.bin", 42_000_000, 0) == "  model.bin 42.0 MB"


def test_cloning_happens_before_building(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    calls = stub_bootstrap(monkeypatch)

    services.setup()

    assert calls.index("clone") < calls.index("build")


def test_setup_skips_steps_whose_artifact_already_exists(monkeypatch, tmp_path):
    vendor = configured_paths(monkeypatch, tmp_path)
    vendor.mkdir(parents=True)
    binary = services.config.WHISPER_BIN
    binary.parent.mkdir(parents=True)
    binary.write_text("bin")
    binary.chmod(0o755)
    services.config.MODELS_DIR.mkdir(parents=True)
    services.config.WHISPER_MODEL_PATH.write_text("model")
    services.config.VAD_MODEL_PATH.write_text("vad")
    services.config.SEG_MODEL_PATH.parent.mkdir(parents=True)
    services.config.SEG_MODEL_PATH.write_text("seg")
    (services.config.MODELS_DIR / "nemo_en_titanet_large.onnx").write_text("emb")
    _write_native_helpers()
    calls = stub_bootstrap(monkeypatch)

    services.setup()

    assert calls == ["tools"]


def test_the_embedding_model_always_downloads_under_its_own_fixed_name(monkeypatch, tmp_path):
    # emb_model can be overridden to point at a model the user supplies
    # themselves; provisioning the default must never land under that name
    configured_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        services.config, "EMB_MODEL_PATH", services.config.MODELS_DIR / "custom.onnx"
    )
    stub_bootstrap(monkeypatch)
    fetched: list = []
    monkeypatch.setattr(
        services.bootstrap,
        "fetch",
        lambda url, dst, tick=lambda read, total: None: fetched.append(dst),
    )

    services.setup()

    assert fetched == [services.config.MODELS_DIR / "nemo_en_titanet_large.onnx"]
    assert not (services.config.MODELS_DIR / "custom.onnx").exists()


def test_a_vad_download_failure_is_tolerated_rather_than_failing_setup(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    calls = stub_bootstrap(monkeypatch, vad_fails=True)
    logged: list = []

    result = services.setup(log=logged.append)

    assert result == "setup complete"
    assert "download-vad-model.sh" in calls
    assert any("VAD download failed" in line for line in logged)


def test_setup_skips_the_capture_helper_away_from_a_mac(monkeypatch, tmp_path):
    # recording a meeting is a macOS-only feature; the rest of the pipeline
    # still installs on any machine rather than failing on a helper it cannot
    # build there
    configured_paths(monkeypatch, tmp_path)
    calls = stub_bootstrap(monkeypatch)
    monkeypatch.setattr(services.sys, "platform", "linux")
    logged: list = []

    services.setup(log=logged.append)

    assert "capture" not in calls
    assert "[7/8] meeting-capture helper — macOS only, skipped" in logged


def test_setup_skips_the_menu_bar_recorder_away_from_a_mac(monkeypatch, tmp_path):
    # the recorder is an AppKit app driving the macOS-only capture helper, so
    # there is nothing to build with anywhere else and nothing it could record
    configured_paths(monkeypatch, tmp_path)
    calls = stub_bootstrap(monkeypatch)
    monkeypatch.setattr(services.sys, "platform", "linux")
    logged: list = []

    services.setup(log=logged.append)

    assert "menubar" not in calls
    assert "[8/8] menu bar recorder — macOS only, skipped" in logged


def test_mock_world_previews_setup_without_touching_the_network_or_disk(monkeypatch, tmp_path):
    # a preview has to fail early the same way a real run would (require_tools
    # stays real) but must never reach git, cmake, curl or an actual download
    configured_paths(monkeypatch, tmp_path)

    def gateway_called(*_a, **_k):
        raise AssertionError("real gateway called")

    monkeypatch.setattr(services.sys, "platform", "darwin")
    monkeypatch.setattr(services.bootstrap, "clone_whisper", gateway_called)
    monkeypatch.setattr(services.bootstrap, "build_whisper", gateway_called)
    monkeypatch.setattr(services.bootstrap, "build_capture", gateway_called)
    monkeypatch.setattr(services.bootstrap, "build_menubar", gateway_called)
    monkeypatch.setattr(services.bootstrap, "download_model_script", gateway_called)
    monkeypatch.setattr(services.bootstrap, "fetch", gateway_called)
    monkeypatch.setattr(services.bootstrap, "fetch_tar_bz2", gateway_called)
    monkeypatch.setattr(services.bootstrap, "require_tools", lambda: True)
    logged: list = []
    downloaded: list = []

    result = services.setup(
        log=logged.append,
        download=downloaded.append,
        world=services.mock_world(sleep=lambda _s: None),
    )

    assert result == "setup complete"
    for n in range(1, 9):
        assert any(line.startswith(f"[{n}/8]") for line in logged)
    assert sum(line.startswith("      done in") for line in logged) == 8
    assert any("%" in line and "MB" in line for line in downloaded)
    assert not services.config.MODELS_DIR.exists()
    assert not services.config.DATA_DIR.exists()
    assert not services.config.UPLOADS_DIR.exists()


def _write_ready_artifacts(skip: str = "") -> None:
    """Puts every artifact ready() checks on disk, except the one named to skip."""
    if skip != "binary":
        binary = services.config.WHISPER_BIN
        binary.parent.mkdir(parents=True)
        binary.write_text("bin")
        binary.chmod(0o755)
    services.config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if skip != "whisper_model":
        services.config.WHISPER_MODEL_PATH.write_text("model")
    if skip != "seg_model":
        services.config.SEG_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        services.config.SEG_MODEL_PATH.write_text("seg")
    if skip != "emb_model":
        services.config.EMB_MODEL_PATH.write_text("emb")


def test_ready_is_true_once_every_required_artifact_is_on_disk(monkeypatch, tmp_path):
    configured_paths(monkeypatch, tmp_path)
    _write_ready_artifacts()

    assert services.ready() is True


@pytest.mark.parametrize("skip", ["binary", "whisper_model", "seg_model", "emb_model"])
def test_ready_is_false_when_one_required_artifact_is_missing(monkeypatch, tmp_path, skip):
    configured_paths(monkeypatch, tmp_path)
    _write_ready_artifacts(skip=skip)

    assert services.ready() is False


def test_durations_read_like_a_person_says_them():
    assert services._duration(None) == "?"
    assert services._duration(0) == "?"
    assert services._duration(42) == "42s"
    assert services._duration(75) == "1m 15s"
    assert services._duration(119.6) == "2m"
    assert services._duration(3600) == "1h"
    assert services._duration(3661) == "1h 1m 1s"
    assert services._duration(90061) == "1d 1h 1m 1s"
