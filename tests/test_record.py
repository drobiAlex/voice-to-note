import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import StubRepo

from voice_to_note import cli, services
from voice_to_note.gateways import GatewayError, audio, capture

# stand-ins for the native helper, which cannot run in a test: it would ask
# macOS for the microphone and the screen. Each speaks the same two words on
# stdout the real one does, and answers a stop the same way.
TAPES = """
import os, signal, sys, time

def stop(_sig, _frame):
    # straight to the descriptor: a print here can land inside the one the main
    # body is already making, which python refuses as a reentrant write
    os.write(1, b"stopped\\n")
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
print("recording", flush=True)
time.sleep(60)
"""

QUITS_AT_ONCE = """
print("recording", flush=True)
"""

REPORTS_LEVELS = """
import os, signal, sys, time

def stop(_sig, _frame):
    # straight to the descriptor: a print here can land inside the one the main
    # body is already making, which python refuses as a reentrant write
    os.write(1, b"stopped\\n")
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
print("recording", flush=True)
print("level\\t-18.2\\t-60.0", flush=True)
print("level\\t-3.0\\t-12.5", flush=True)
print("a word from a newer helper than this one", flush=True)
time.sleep(60)
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


def heard(readings: list, count: int) -> list:
    """Waits for the levels the helper printed to reach the callback, which
    they do on a thread of their own — a test that looked once would read an
    empty list and call that the answer."""
    deadline = time.time() + 5
    while len(readings) < count and time.time() < deadline:
        time.sleep(0.01)
    return readings


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


# --- watching the levels while it tapes ------------------------------------


def test_the_helper_is_asked_to_measure_only_when_somebody_is_listening(tmp_path, monkeypatch):
    # measuring costs a pass over every buffer of a meeting, and a recording
    # nobody is watching must not pay for what it will never show
    log = tmp_path / "argv.json"
    monkeypatch.setattr(
        capture.config, "CAPTURE_BIN", helper(tmp_path, records_how_it_was_called(log))
    )
    system, mic = tmp_path / "system.wav", tmp_path / "mic.wav"

    capture.start(system, mic).stop()
    assert "--levels" not in json.loads(log.read_text())

    capture.start(system, mic, levels=lambda _system_db, _mic_db: None).stop()
    assert json.loads(log.read_text())[-1] == "--levels"


def test_the_levels_the_helper_reports_arrive_as_two_numbers_system_first(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, REPORTS_LEVELS))
    readings: list = []

    recording = capture.start(
        tmp_path / "system.wav",
        tmp_path / "mic.wav",
        levels=lambda system_db, mic_db: readings.append((system_db, mic_db)),
    )

    assert heard(readings, 2) == [(-18.2, -60.0), (-3.0, -12.5)]
    recording.stop()


def test_stopping_returns_on_the_last_word_however_many_levels_came_before_it(
    tmp_path, monkeypatch
):
    # ten lines a second stand between the stop and the word that answers it,
    # and a stop that gave up at the first of them would leave the two wav
    # files unfinished
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, REPORTS_LEVELS))
    recording = capture.start(
        tmp_path / "system.wav", tmp_path / "mic.wav", levels=lambda _s, _m: None
    )

    recording.stop()

    assert recording.wait() == 0


def test_a_meter_that_blows_up_costs_only_the_reading_it_was_handed(tmp_path, monkeypatch):
    # levels are cosmetic and the recording is not: a caller whose meter raised
    # must not leave the helper's output unread, since that is what fills the
    # pipe and stops a meeting mid-sentence
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, REPORTS_LEVELS))
    readings: list = []

    def explode(system_db: float, mic_db: float) -> None:
        readings.append((system_db, mic_db))
        raise RuntimeError("the meter is on fire")

    recording = capture.start(tmp_path / "system.wav", tmp_path / "mic.wav", levels=explode)

    assert heard(readings, 2) == [(-18.2, -60.0), (-3.0, -12.5)]
    recording.stop()
    assert recording.wait() == 0


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

    def start(_system, _mic, output_uid=None, input_uid=None, levels=None):
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


class ReportedOneLevel:
    """A recording that reports a single reading and is then over, standing in
    for the helper where a test cares what was on screen while it taped."""

    def __init__(self, levels) -> None:
        self._levels = levels

    def wait(self) -> int:
        if self._levels is not None:
            self._levels(-18.24, -60.0)
        return 0

    def stop(self) -> None:
        pass


def taped_one_reading(monkeypatch) -> dict:
    """Runs a recording that reports one level and stops, reporting who was
    told about it — which is decided before a single buffer is measured."""
    monkeypatch.setattr(sys, "platform", "darwin")
    seen: dict = {}

    def start(_system, _mic, output_uid=None, input_uid=None, levels=None):
        seen["levels"] = levels
        return ReportedOneLevel(levels)

    monkeypatch.setattr(cli.capture, "start", start)
    return seen


def test_asking_for_levels_prints_the_numbers_themselves_for_another_program(
    monkeypatch, capsys
):
    # the flag exists for whatever wraps this command and draws its own meter,
    # so what it gets has to be parseable rather than pretty
    taped_one_reading(monkeypatch)

    with pytest.raises(SystemExit):
        run(monkeypatch, StubRepo(), "record", "--levels")

    assert "level\t-18.2\t-60.0\n" in capsys.readouterr().err


def test_a_terminal_is_shown_a_meter_redrawn_over_itself(monkeypatch, capsys):
    taped_one_reading(monkeypatch)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    with pytest.raises(SystemExit):
        run(monkeypatch, StubRepo(), "record")

    # the meter is ended by a newline of its own: the line after it would
    # otherwise be printed over the bars and read as part of them
    assert "\r" + services.meter_line(-18.24, -60.0) + "\n" in capsys.readouterr().err


def test_a_recording_nobody_can_watch_is_not_measured_at_all(monkeypatch):
    # output redirected into a file has nowhere to draw a meter, and measuring
    # for a meter that is never drawn costs a meeting's worth of buffers
    seen = taped_one_reading(monkeypatch)

    with pytest.raises(SystemExit):
        run(monkeypatch, StubRepo(), "record")

    assert seen["levels"] is None
