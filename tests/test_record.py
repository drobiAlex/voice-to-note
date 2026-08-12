import subprocess
import sys
from pathlib import Path

import pytest
from conftest import StubRepo

from voice_to_note import cli
from voice_to_note.gateways import GatewayError, audio, capture

# stand-ins for the native helper, which cannot run in a test: it would ask
# macOS for the microphone and the screen. Each speaks the same two words on
# stdout the real one does, and answers a stop the same way.
TAPES = """
import signal, sys, time

def stop(_sig, _frame):
    print("stopped", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
print("recording", flush=True)
time.sleep(60)
"""

QUITS_AT_ONCE = """
print("recording", flush=True)
"""

DENIES_THE_MICROPHONE = """
import sys

sys.stderr.write("open System Settings\\n")
sys.exit(3)
"""


def helper(tmp_path: Path, body: str) -> Path:
    """A fake vtn-capture on disk, run by the same interpreter as the test."""
    path = tmp_path / "vtn-capture"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def run(monkeypatch, repo, *argv) -> None:
    """Runs the real command line against a test database."""
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", *argv])
    cli.main()


# --- taping a meeting ------------------------------------------------------


def test_a_recording_starts_only_once_the_helper_is_actually_taping(tmp_path, monkeypatch):
    # the caller tells the user the meeting is being recorded, so start() must
    # not come back until that is true
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, TAPES))
    tracks = tmp_path / "tracks"

    recording = capture.start(tracks / "system.wav", tracks / "mic.wav")

    assert tracks.is_dir()
    recording.stop()
    assert recording.wait() == 0


def test_stopping_a_helper_that_already_quit_is_not_an_error(tmp_path, monkeypatch):
    # a Ctrl+C in a terminal signals the whole foreground group, so the helper
    # has usually stopped itself before stop() is ever reached
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, QUITS_AT_ONCE))
    recording = capture.start(tmp_path / "system.wav", tmp_path / "mic.wav")
    assert recording.wait() == 0

    recording.stop()


def test_a_refused_microphone_is_reported_as_the_setting_to_change(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capture.config, "CAPTURE_BIN", helper(tmp_path, DENIES_THE_MICROPHONE)
    )

    with pytest.raises(GatewayError, match="[Mm]icrophone"):
        capture.start(tmp_path / "system.wav", tmp_path / "mic.wav")


def test_recording_before_setup_has_built_the_helper_says_to_run_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", tmp_path / "never-built")

    with pytest.raises(GatewayError, match="vtn setup"):
        capture.start(tmp_path / "system.wav", tmp_path / "mic.wav")


# --- folding the two tracks into one recording -----------------------------


def test_merging_mixes_both_tracks_into_the_one_file_the_pipeline_reads(tmp_path, monkeypatch):
    seen: dict = {}

    def merge(cmd, **_kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(audio.subprocess, "run", merge)
    system, mic = tmp_path / "system.wav", tmp_path / "mic.wav"
    dst = tmp_path / "recordings" / "meeting.m4a"

    audio.merge_tracks(system, mic, dst)

    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    assert cmd.count("-i") == 2
    assert str(system) in cmd
    assert str(mic) in cmd
    assert "amix=inputs=2:duration=longest" in cmd
    assert cmd[-1] == str(dst)
    assert dst.parent.is_dir()


def test_a_failed_merge_names_both_tracks_so_the_meeting_can_be_found(tmp_path, monkeypatch):
    # the raw tracks are the only copy of the meeting until the merge succeeds
    def fail(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "amix: invalid argument")

    monkeypatch.setattr(audio.subprocess, "run", fail)
    system, mic = tmp_path / "system.wav", tmp_path / "mic.wav"

    with pytest.raises(GatewayError) as err:
        audio.merge_tracks(system, mic, tmp_path / "meeting.m4a")

    assert str(system) in str(err.value)
    assert str(mic) in str(err.value)


# --- the record command ----------------------------------------------------


def test_recording_anywhere_but_a_mac_says_so_instead_of_failing_oddly(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "record")

    assert err.value.code == "meeting recording is macOS-only"


def test_an_unknown_template_is_refused_before_the_tape_ever_rolls(monkeypatch):
    # nobody should sit through a meeting to be told a template name was wrong
    monkeypatch.setattr(sys, "platform", "darwin")

    def never(*_args, **_kwargs):
        raise AssertionError("recording must not start for an unknown template")

    monkeypatch.setattr(cli.capture, "start", never)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "record", "--template", "bogus")

    assert "unknown note template" in str(err.value.code)
