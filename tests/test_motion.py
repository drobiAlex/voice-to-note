"""Exercise the shipped Swift solver with the app's actual motion constants."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="Swift compiler is unavailable")
def test_the_shipped_spring_settles_with_one_visible_overshoot_at_both_refresh_rates(tmp_path):
    """Compile production math so acceptance checks detect solver or tuning changes."""
    native = Path(__file__).resolve().parents[1] / "src/voice_to_note/native"
    source = (native / "menubar.swift").read_text()
    # The Foundation-only constants are isolated from AppKit for this harness;
    # their contents are compiled as Swift, not asserted as source strings.
    constants = source.split("enum PuckMotion {", 1)[1].split("\n}\n", 1)[0]
    harness = (Path(__file__).parent / "motion_harness.swift").read_text()
    main = tmp_path / "main.swift"
    main.write_text("import Foundation\nenum PuckMotion {" + constants + "\n}\n" + harness)
    binary = tmp_path / "motion-check"
    subprocess.run(
        ["swiftc", "-O", str(native / "springs.swift"), str(main), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    print(result.stdout)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="AppKit requires the macOS Swift compiler",
)
def test_the_full_recorder_compiles_with_optimized_vendored_physics(tmp_path):
    """Link the real app under production optimization without launching it."""
    native = Path(__file__).resolve().parents[1] / "src/voice_to_note/native"
    subprocess.run(
        ["swiftc", str(native / "menubar.swift"), str(native / "springs.swift"),
         "-O", "-o", str(tmp_path / "vtn-menubar")],
        check=True, capture_output=True, text=True,
    )
