import json
import subprocess
import sys

import pytest

from voice_to_note import cli, services
from voice_to_note.gateways import GatewayError, audio, llm
from voice_to_note.transforms.segments import segments_from_whisper


def fake_run(monkeypatch, module, *, returncode=0, stdout="", stderr=""):
    """Stands in for a real command, keeping the two subprocess.run rules that
    decide what the caller can see: check= raises, and output only comes back
    when it was asked for."""

    def run(cmd, **kwargs):
        captured = kwargs.get("capture_output") or kwargs.get("stderr") == subprocess.PIPE
        proc = subprocess.CompletedProcess(
            cmd, returncode, stdout if captured else None, stderr if captured else None
        )
        if kwargs.get("check") and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, proc.stdout, proc.stderr)
        return proc

    monkeypatch.setattr(module.subprocess, "run", run)


def fake_missing_binary(monkeypatch, module) -> None:
    """Stands in for a machine where the tool was never installed at all."""

    def run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(module.subprocess, "run", run)


class FakeResponse:
    """An HTTP 200 whose body is whatever the test wants the model to have said."""

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def fake_ollama(monkeypatch, body: bytes) -> None:
    """Answers the next Ollama call with a canned body, without touching a socket."""
    monkeypatch.setattr(
        llm.urllib.request, "urlopen", lambda _req, timeout=None: FakeResponse(body)
    )


def boom(message: str, error: type[Exception]):
    """A command that fails, with the caller choosing whether that failure reads
    as a setup problem or as a bug."""

    def fn(*args, **kwargs):
        raise error(message)

    return fn


# --- audio conversion ---------------------------------------------------


def test_a_recording_ffmpeg_cannot_convert_reports_what_ffmpeg_said(tmp_path, monkeypatch):
    stderr = "warning: ignoring unknown tag\n" * 200 + "Invalid data found in input"
    fake_run(monkeypatch, audio, returncode=1, stderr=stderr)

    with pytest.raises(GatewayError) as err:
        audio.to_wav16k(tmp_path / "memo.m4a", tmp_path / "uploads" / "memo.wav")

    message = str(err.value)
    assert "ffmpeg" in message
    assert "Invalid data found in input" in message
    # the tail is the part that says why; the preceding noise must not be dumped whole
    assert len(message) < len(stderr)


def test_a_machine_without_ffmpeg_says_how_to_get_it(tmp_path, monkeypatch):
    fake_missing_binary(monkeypatch, audio)

    with pytest.raises(GatewayError) as err:
        audio.to_wav16k(tmp_path / "memo.m4a", tmp_path / "uploads" / "memo.wav")

    message = str(err.value)
    assert "ffmpeg" in message
    assert "install" in message


def test_a_machine_without_ffprobe_says_how_to_get_it(tmp_path, monkeypatch):
    fake_missing_binary(monkeypatch, audio)

    with pytest.raises(GatewayError) as err:
        audio.duration_seconds(tmp_path / "standup.m4a")

    message = str(err.value)
    assert "ffprobe" in message
    assert "install" in message


def test_a_recording_ffprobe_cannot_read_reports_what_ffprobe_said(tmp_path, monkeypatch):
    fake_run(monkeypatch, audio, returncode=1, stderr="Invalid data found in input")

    with pytest.raises(GatewayError) as err:
        audio.duration_seconds(tmp_path / "standup.m4a")

    message = str(err.value)
    assert "standup.m4a" in message
    assert "Invalid data found in input" in message


@pytest.mark.parametrize("stdout", ["", "   ", "ffprobe version 7.1\n"])
def test_unreadable_ffprobe_output_names_the_file_it_could_not_measure(
    tmp_path, monkeypatch, stdout
):
    # without a duration there is no transcription budget, so the recording is unusable
    fake_run(monkeypatch, audio, stdout=stdout)
    path = tmp_path / "standup.m4a"

    with pytest.raises(GatewayError) as err:
        audio.duration_seconds(path)

    assert "standup.m4a" in str(err.value)


@pytest.mark.parametrize("stdout", ["{}", '{"format": {}}', '{"streams": []}', "[]"])
def test_ffprobe_output_without_a_duration_is_reported_as_a_missing_duration(
    tmp_path, monkeypatch, stdout
):
    fake_run(monkeypatch, audio, stdout=stdout)

    with pytest.raises(GatewayError, match="duration"):
        audio.duration_seconds(tmp_path / "standup.m4a")


# --- ollama ------------------------------------------------------------


def test_an_ollama_error_reply_demotes_the_backend_instead_of_crashing(monkeypatch):
    # a 200 carrying an error body is how ollama reports a model that was never pulled
    fake_ollama(monkeypatch, json.dumps({"error": 'model "qwen3:8b" not found'}).encode())

    with pytest.raises(llm.BackendError) as err:
        llm.ollama_complete("summarise this")

    assert "not found" in str(err.value)


def test_a_reply_that_is_not_ollama_json_demotes_the_backend(monkeypatch):
    fake_ollama(monkeypatch, b"<html><body>502 Bad Gateway</body></html>")

    with pytest.raises(llm.BackendError):
        llm.ollama_complete("summarise this")


def test_an_ollama_reply_of_the_wrong_json_type_demotes_the_backend(monkeypatch):
    fake_ollama(monkeypatch, b"[]")

    with pytest.raises(llm.BackendError):
        llm.ollama_complete("summarise this")


# --- whisper output ----------------------------------------------------


def test_a_transcription_segment_without_text_names_the_missing_field():
    # whisper writes its JSON to a temp dir the user never sees, so the error is
    # the only diagnosis available when a build changes shape
    raw = {"transcription": [{"offsets": {"from": 0, "to": 1500}}]}

    with pytest.raises(ValueError, match="text"):
        segments_from_whisper(raw)


def test_a_transcription_segment_without_offsets_names_the_missing_field():
    raw = {"transcription": [{"text": "Hello there."}]}

    with pytest.raises(ValueError, match="offsets"):
        segments_from_whisper(raw)


@pytest.mark.parametrize("offsets", [{"to": 1500}, {"from": 0}])
def test_a_transcription_segment_with_half_its_timing_names_the_missing_bound(offsets):
    raw = {"transcription": [{"text": "Hello there.", "offsets": offsets}]}

    with pytest.raises(ValueError, match="from|to"):
        segments_from_whisper(raw)


def test_a_transcription_entry_that_is_not_a_segment_names_its_position():
    good = {"text": "Hello there.", "offsets": {"from": 0, "to": 1500}}
    raw = {"transcription": [good, "Second one."]}

    with pytest.raises(ValueError, match="segment 1"):
        segments_from_whisper(raw)


# --- the command line --------------------------------------------------


def test_a_missing_model_ends_the_command_with_a_message_not_a_traceback(monkeypatch):
    failure = "segmentation model missing — run ./run.sh"
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: object())
    monkeypatch.setattr(cli.services, "rediarize", boom(failure, GatewayError))
    monkeypatch.setattr(sys, "argv", ["vtn", "diarize", "3"])

    with pytest.raises(SystemExit) as err:
        cli.main()

    assert err.value.code == failure


def test_a_failed_conversion_ends_process_with_a_message_not_a_traceback(tmp_path, monkeypatch):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    failure = "whisper-cli not built — run ./run.sh first"
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: object())
    monkeypatch.setattr(cli.services, "process_memo", boom(failure, GatewayError))
    monkeypatch.setattr(sys, "argv", ["vtn", "process", str(src)])

    with pytest.raises(SystemExit) as err:
        cli.main()

    assert err.value.code == failure


def test_processing_without_ffmpeg_installed_ends_with_a_message_not_a_traceback(
    tmp_path, monkeypatch
):
    # the whole seam end to end: a real gateway failure, through services, out of main()
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: object())
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    fake_missing_binary(monkeypatch, audio)
    monkeypatch.setattr(sys, "argv", ["vtn", "process", str(src)])

    with pytest.raises(SystemExit) as err:
        cli.main()

    assert "ffmpeg" in str(err.value.code)


def test_a_bug_inside_a_command_keeps_its_traceback(monkeypatch):
    # a plain RuntimeError is a defect, not a setup problem: hiding it behind a
    # one-line exit would cost the developer the stack that locates it
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: object())
    monkeypatch.setattr(cli.services, "rediarize", boom("index out of range", RuntimeError))
    monkeypatch.setattr(sys, "argv", ["vtn", "diarize", "3"])

    with pytest.raises(RuntimeError, match="index out of range"):
        cli.main()


def test_an_unimplemented_command_keeps_its_traceback(monkeypatch):
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: object())
    monkeypatch.setattr(cli.services, "rediarize", boom("todo", NotImplementedError))
    monkeypatch.setattr(sys, "argv", ["vtn", "diarize", "3"])

    with pytest.raises(NotImplementedError):
        cli.main()
