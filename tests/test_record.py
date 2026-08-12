import json
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

LISTS_DEVICES = """
print("in\\tBuiltInMicrophoneDevice\\tMacBook Pro Microphone")
print("out\\tBuiltInSpeakerDevice\\tMacBook Pro Speakers")
"""

CANNOT_READ_DEVICES = """
import sys

sys.stderr.write("cannot read this Mac's audio devices\\n")
sys.exit(4)
"""


def records_how_it_was_called(log: Path) -> str:
    """A fake helper that writes down the arguments it was given and stops at
    once, for a test about what the recording was asked for rather than what
    came of it."""
    return f"""
import json, sys

with open({str(log)!r}, "w") as f:
    json.dump(sys.argv[1:], f)
print("recording", flush=True)
"""


class StoppedAtOnce:
    """A recording that is over as soon as it begins, standing in for the
    helper where a test only cares how the recording was set up."""

    def wait(self) -> int:
        return 0

    def stop(self) -> None:
        pass


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


# --- choosing what to record from ------------------------------------------


def test_choosing_no_device_leaves_both_sides_to_whatever_the_mac_is_set_to(
    tmp_path, monkeypatch
):
    # the common case is that nobody has been asked to choose, and that has to
    # keep recording the whole system mix and the default microphone
    log = tmp_path / "argv.json"
    monkeypatch.setattr(
        capture.config, "CAPTURE_BIN", helper(tmp_path, records_how_it_was_called(log))
    )
    system, mic = tmp_path / "system.wav", tmp_path / "mic.wav"

    capture.start(system, mic).stop()

    assert json.loads(log.read_text()) == [str(system), str(mic)]


def test_a_chosen_output_and_microphone_are_both_named_to_the_helper(tmp_path, monkeypatch):
    log = tmp_path / "argv.json"
    monkeypatch.setattr(
        capture.config, "CAPTURE_BIN", helper(tmp_path, records_how_it_was_called(log))
    )

    capture.start(
        tmp_path / "system.wav",
        tmp_path / "mic.wav",
        output_uid="BlackHole2ch",
        input_uid="Scarlett2i2",
    ).stop()

    assert json.loads(log.read_text())[2:] == [
        "--output-uid",
        "BlackHole2ch",
        "--input-uid",
        "Scarlett2i2",
    ]


def test_choosing_one_side_says_nothing_about_the_other(tmp_path, monkeypatch):
    # naming a microphone must not also pin the output: an unasked-for choice
    # would record one device's playback where the whole mix was wanted
    log = tmp_path / "argv.json"
    monkeypatch.setattr(
        capture.config, "CAPTURE_BIN", helper(tmp_path, records_how_it_was_called(log))
    )

    capture.start(tmp_path / "system.wav", tmp_path / "mic.wav", input_uid="Scarlett2i2").stop()

    assert json.loads(log.read_text())[2:] == ["--input-uid", "Scarlett2i2"]


def test_listing_devices_offers_every_direction_a_device_works_in(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, LISTS_DEVICES))

    assert capture.devices() == (
        "in\tBuiltInMicrophoneDevice\tMacBook Pro Microphone\n"
        "out\tBuiltInSpeakerDevice\tMacBook Pro Speakers\n"
    )


def test_devices_that_cannot_be_read_are_reported_rather_than_shown_as_none(
    tmp_path, monkeypatch
):
    # a failed read and a Mac with no devices both print nothing, and a picker
    # offering nothing at all would look like a correct answer
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, CANNOT_READ_DEVICES))

    with pytest.raises(GatewayError, match="cannot read this Mac's audio devices"):
        capture.devices()


def test_listing_devices_before_setup_has_built_the_helper_says_to_run_setup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", tmp_path / "never-built")
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "devices")

    assert "vtn setup" in str(err.value.code)


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


def taped_nothing(monkeypatch) -> dict:
    """Runs a recording that captures nothing, reporting only which devices it
    was pointed at. Ending with empty tracks is what `record` exits over, and
    that exit is what keeps the test off the rest of the pipeline."""
    monkeypatch.setattr(sys, "platform", "darwin")
    seen: dict = {}

    def start(_system, _mic, output_uid=None, input_uid=None):
        seen.update(output_uid=output_uid, input_uid=input_uid)
        return StoppedAtOnce()

    monkeypatch.setattr(cli.capture, "start", start)
    return seen


def test_the_devices_a_person_chose_are_the_ones_the_meeting_is_taped_from(monkeypatch):
    seen = taped_nothing(monkeypatch)

    with pytest.raises(SystemExit):
        run(
            monkeypatch,
            StubRepo(),
            "record",
            "--output-device",
            "BlackHole2ch",
            "--input-device",
            "Scarlett2i2",
        )

    assert seen == {"output_uid": "BlackHole2ch", "input_uid": "Scarlett2i2"}


def test_recording_without_choosing_devices_pins_neither(monkeypatch):
    seen = taped_nothing(monkeypatch)

    with pytest.raises(SystemExit):
        run(monkeypatch, StubRepo(), "record")

    assert seen == {"output_uid": None, "input_uid": None}
