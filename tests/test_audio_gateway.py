import json
import os
import subprocess
from datetime import UTC, datetime

import pytest

from voice_to_note.gateways import GatewayError, audio


def fake_ffprobe(monkeypatch, *, stdout="", returncode=0, stderr="") -> None:
    """Answers the next ffprobe call with a canned reply, so a test about what
    the gateway makes of a container's tags runs on a machine that has no
    ffprobe and no recording to point it at."""
    monkeypatch.setattr(
        audio.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, returncode, stdout, stderr),
    )


def tagged(creation_time: str) -> str:
    """What ffprobe prints for a container carrying this creation time."""
    return json.dumps({"format": {"tags": {"creation_time": creation_time}}})


def written_at(path, when: datetime) -> None:
    """A file the filesystem says was last written at this moment."""
    path.write_bytes(b"fake audio")
    os.utime(path, (when.timestamp(), when.timestamp()))


# --- when a recording says it was made -------------------------------------


def test_the_creation_time_a_container_carries_is_what_a_recording_is_dated_by(
    tmp_path, monkeypatch
):
    fake_ffprobe(monkeypatch, stdout=tagged("2026-08-17T06:01:22.000000Z"))

    assert audio.recorded_at(tmp_path / "standup.m4a") == "2026-08-17T06:01:22Z"


@pytest.mark.parametrize(
    "creation_time",
    [
        "2026-08-17T06:01:22Z",
        "2026-08-17T06:01:22.000000Z",
        "2026-08-17T06:01:22.847391Z",
        "2026-08-17T08:01:22+02:00",
        "2026-08-17T06:01:22",
    ],
)
def test_one_moment_reads_the_same_however_the_container_spelled_it(
    tmp_path, monkeypatch, creation_time
):
    # this stamp is what a second import is recognised by, so two recorders
    # writing the same instant differently must not read as two instants
    fake_ffprobe(monkeypatch, stdout=tagged(creation_time))

    assert audio.recorded_at(tmp_path / "standup.m4a") == "2026-08-17T06:01:22Z"


@pytest.mark.parametrize(
    "stdout", ["{}", '{"format": {}}', '{"format": {"tags": {}}}', "not json at all"]
)
def test_a_container_that_never_said_when_falls_back_to_when_the_file_was_written(
    tmp_path, monkeypatch, stdout
):
    # a weaker answer, since copying a file can rewrite it, but always an answer
    fake_ffprobe(monkeypatch, stdout=stdout)
    src = tmp_path / "standup.m4a"
    written_at(src, datetime(2026, 8, 17, 6, 1, 22, tzinfo=UTC))

    assert audio.recorded_at(src) == "2026-08-17T06:01:22Z"


def test_a_creation_time_nothing_can_read_falls_back_rather_than_failing(tmp_path, monkeypatch):
    # what ffmpeg writes where a container's creation time was never set; a
    # recording is not broken for carrying it, so this is not an error
    fake_ffprobe(monkeypatch, stdout=tagged("0000-00-00T00:00:00.000000Z"))
    src = tmp_path / "standup.m4a"
    written_at(src, datetime(2026, 8, 17, 6, 1, 22, tzinfo=UTC))

    assert audio.recorded_at(src) == "2026-08-17T06:01:22Z"


def test_a_recording_ffprobe_cannot_read_at_all_names_the_file_and_says_why(
    tmp_path, monkeypatch
):
    fake_ffprobe(monkeypatch, returncode=1, stderr="Invalid data found in input")

    with pytest.raises(GatewayError) as err:
        audio.recorded_at(tmp_path / "standup.m4a")

    message = str(err.value)
    assert "standup.m4a" in message
    assert "Invalid data found in input" in message


def test_dating_a_recording_without_ffprobe_installed_says_how_to_get_it(tmp_path, monkeypatch):
    def missing(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(audio.subprocess, "run", missing)

    with pytest.raises(GatewayError) as err:
        audio.recorded_at(tmp_path / "standup.m4a")

    message = str(err.value)
    assert "ffprobe" in message
    assert "install" in message
