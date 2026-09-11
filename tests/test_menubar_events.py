"""Exercise the recorder's Foundation-only stderr event boundary."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="Swift compiler is unavailable")
def test_fragmented_machine_events_keep_progress_and_terminal_meaning(tmp_path):
    """Compile the decoder so a pipe split cannot turn a transcript into a ready note."""
    native = Path(__file__).resolve().parents[1] / "src/voice_to_note/native" / "menubar.swift"
    source = native.read_text()
    event_source = source.split("enum RecorderEvent: Equatable {", 1)[1].split(
        "/// A device the Mac can record", 1
    )[0]
    harness = """
import Foundation

var decoder = RecorderEventDecoder()
let first = decoder.receive(Data("ordinary status\\nvtn-event {\\\"event\\\":\\\"stage\\\",\\\"stage\\\":\\\"trans".utf8))
precondition(first.count == 1 && first[0].event == nil)
let second = decoder.receive(Data("cribing\\\"}\\nvtn-event {\\\"event\\\":\\\"transcript_ready\\\",\\\"memo_id\\\":7}\\nvtn-event {\\\"event\\\":\\\"ready\\\",\\\"memo_id\\\":7}\\n".utf8))
precondition(second.count == 3)
precondition(second[0].event == .stage("transcribing"))
precondition(second[1].event == .transcriptReady(7))
precondition(second[2].event == .ready(7))
let cancellable = decoder.receive(Data("vtn-event {\\\"event\\\":\\\"cancellable\\\",\\\"pgid\\\":77}\\n".utf8))
precondition(cancellable.count == 1 && cancellable[0].event == .cancellable(77))
let emoji = Array("vtn-event {\\\"event\\\":\\\"failed\\\",\\\"kind\\\":\\\"notes\\\",\\\"memo_id\\\":7,\\\"message\\\":\\\"offline ✓\\\"}".utf8)
let failed = decoder.receive(Data(emoji.dropLast(2)))
precondition(failed.isEmpty)
precondition(decoder.receive(Data(emoji.suffix(2))).isEmpty)
if case let .failed(kind, memoID, message)? = decoder.finish()?.event {
    precondition(kind == "notes" && memoID == 7 && message == "offline ✓")
} else { fatalError("failed event was not retained to EOF") }
"""
    main = tmp_path / "main.swift"
    main.write_text("import Foundation\nenum RecorderEvent: Equatable {" + event_source + harness)
    binary = tmp_path / "events-check"
    compiled = subprocess.run(
        ["swiftc", str(main), "-o", str(binary)], capture_output=True, text=True
    )
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
