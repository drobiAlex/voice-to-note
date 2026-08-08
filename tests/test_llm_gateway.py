import json
import urllib.error

import pytest

from voice_to_note import config
from voice_to_note.gateways import llm
from voice_to_note.transforms.notes import SCHEMA

TRANSCRIPT = "[00:00] Alice: We ship on Friday."


class FakeResponse:
    """A reply from an ollama that is running, whatever it chose to say."""

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def fake_tags(monkeypatch, body: bytes) -> None:
    """Answers the model-list call with a canned body, without touching a socket."""
    monkeypatch.setattr(
        llm.urllib.request, "urlopen", lambda _url, timeout=None: FakeResponse(body)
    )


def unreachable(monkeypatch, error: Exception) -> None:
    """Stands in for an ollama that is not running, or not reachable."""

    def urlopen(_url, timeout=None):
        raise error

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)


def tags(*names: str) -> bytes:
    """The model list exactly as ollama's /api/tags reports it."""
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


# --- the prompts ---------------------------------------------------------


def test_the_notes_prompt_carries_the_transcript_and_the_shape_it_wants():
    prompt = llm.notes_prompt(TRANSCRIPT)

    assert TRANSCRIPT in prompt
    assert "JSON" in prompt


def test_the_notes_prompt_asks_for_every_section_the_parser_requires():
    # the prompt and the parser have to agree, or every extraction fails validation
    prompt = llm.notes_prompt(TRANSCRIPT)

    assert [key for key in SCHEMA["required"] if f'"{key}"' not in prompt] == []


def test_the_ask_prompt_carries_both_the_question_and_the_transcript():
    prompt = llm.ask_prompt(TRANSCRIPT, "When do they ship?")

    assert "When do they ship?" in prompt
    assert TRANSCRIPT in prompt


def test_a_transcript_containing_braces_still_makes_a_prompt():
    # speech transcribed as "{" would break a prompt built by formatting twice
    prompt = llm.ask_prompt("[00:00] Alice: the cost is {5} dollars", "How much?")

    assert "{5}" in prompt


# --- is the local fallback usable ----------------------------------------


def test_ollama_is_available_once_the_model_is_pulled(monkeypatch):
    fake_tags(monkeypatch, tags(config.OLLAMA_MODEL))

    assert llm.ollama_available() is True


def test_ollama_is_available_when_the_pulled_model_carries_a_tag(monkeypatch):
    fake_tags(monkeypatch, tags(f"{config.OLLAMA_MODEL}-instruct"))

    assert llm.ollama_available() is True


def test_ollama_is_unavailable_when_the_model_was_never_pulled(monkeypatch):
    fake_tags(monkeypatch, tags("some-other-model:1b"))

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_it_has_pulled_nothing(monkeypatch):
    fake_tags(monkeypatch, b'{"models": []}')

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_nothing_is_listening(monkeypatch):
    unreachable(monkeypatch, urllib.error.URLError("connection refused"))

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_the_model_list_is_not_json(monkeypatch):
    fake_tags(monkeypatch, b"<html><body>502 Bad Gateway</body></html>")

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_the_reply_is_json_of_the_wrong_shape(monkeypatch):
    fake_tags(monkeypatch, b"[]")

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_a_listed_model_has_no_name(monkeypatch):
    fake_tags(monkeypatch, b'{"models": [{"name": null}]}')

    assert llm.ollama_available() is False


def test_ollama_is_unavailable_when_the_model_list_has_no_names(monkeypatch):
    fake_tags(monkeypatch, b'{"models": [{"model": "qwen3:8b"}]}')

    assert llm.ollama_available() is False


# --- talking to claude ---------------------------------------------------


def test_a_claude_that_vanished_after_the_check_is_a_backend_failure(monkeypatch):
    # claude_available() passed a moment ago; the binary can still be gone by the
    # time we call it, and that must demote the backend rather than end the run
    def run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr(llm.subprocess, "run", run)

    with pytest.raises(llm.BackendError, match="claude"):
        llm.claude_complete("summarise this")


def test_a_failing_claude_call_reports_what_it_printed(monkeypatch):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda *a, **k: llm.subprocess.CompletedProcess(
            a[0], 1, "", "Invalid API key · Please run /login"
        ),
    )

    with pytest.raises(llm.BackendError, match="/login"):
        llm.claude_complete("summarise this")
