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
    contour = source.split("struct PuckEdgeContour {", 1)[1].split(
        "/// The recorder as a small disc", 1
    )[0]
    harness = (Path(__file__).parent / "motion_harness.swift").read_text()
    main = tmp_path / "main.swift"
    main.write_text(
        "import Foundation\nenum PuckMotion {" + constants + "\n}\n"
        + "struct PuckEdgeContour {" + contour + harness
    )
    binary = tmp_path / "motion-check"
    compiled = subprocess.run(
        ["swiftc", "-O", str(native / "springs.swift"), str(main), "-o", str(binary)],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
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


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="AppKit requires the macOS Swift compiler",
)
def test_control_masks_use_the_production_body_path_in_local_layer_coordinates(tmp_path):
    """Exercise the Core Animation mask used when a puck contour is only partly shown."""
    native = Path(__file__).resolve().parents[1] / "src/voice_to_note/native" / "menubar.swift"
    source = native.read_text()
    mask_source = source.split("struct PuckControlMask {", 1)[1].split(
        "/// The one button", 1
    )[0]
    harness = """
import AppKit

let body = CGPath(ellipseIn: NSRect(x: 10, y: 15, width: 160, height: 160), transform: nil)
let controlFrame = NSRect(x: 146, y: 70, width: 44, height: 44)
let localBody = PuckControlMask.path(for: body, in: controlFrame)
let canvas = CALayer()
canvas.frame = NSRect(x: 0, y: 0, width: 200, height: 200)
let control = CALayer()
control.frame = controlFrame
control.backgroundColor = NSColor.red.cgColor
canvas.addSublayer(control)
let mask = CAShapeLayer()
mask.frame = control.bounds
mask.path = localBody
control.mask = mask
precondition(control.mask === mask, "Control did not retain its contour mask")
let maskPath = mask.path!
for global in [NSPoint(x: 150, y: 90), NSPoint(x: 169, y: 92), NSPoint(x: 185, y: 92)] {
    let local = NSPoint(x: global.x - controlFrame.minX, y: global.y - controlFrame.minY)
    precondition(body.contains(global) == maskPath.contains(local),
                 "Mask no longer maps the body contour into control coordinates")
}
precondition(maskPath.contains(NSPoint(x: 4, y: 20)), "Visible control pixel was clipped")
precondition(!maskPath.contains(NSPoint(x: 39, y: 22)), "Outside control pixel leaked past contour")
let side = 200
let bytesPerRow = side * 4
let pixels = UnsafeMutablePointer<UInt8>.allocate(capacity: side * bytesPerRow)
pixels.initialize(repeating: 0, count: side * bytesPerRow)
defer { pixels.deinitialize(count: side * bytesPerRow); pixels.deallocate() }
let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
guard let bitmap = CGContext(data: pixels, width: side, height: side, bitsPerComponent: 8,
                             bytesPerRow: bytesPerRow, space: CGColorSpaceCreateDeviceRGB(),
                             bitmapInfo: bitmapInfo) else { fatalError("Could not make bitmap") }
canvas.render(in: bitmap)
func alpha(at point: NSPoint) -> UInt8 {
    pixels[(Int(point.y) * bytesPerRow) + (Int(point.x) * 4) + 3]
}
precondition(alpha(at: NSPoint(x: 150, y: 90)) > 0, "Visible control pixel did not render")
precondition(alpha(at: NSPoint(x: 185, y: 92)) == 0, "Masked control pixel rendered outside contour")
"""
    main = tmp_path / "main.swift"
    main.write_text("import AppKit\nstruct PuckControlMask {" + mask_source + harness)
    binary = tmp_path / "mask-check"
    compiled = subprocess.run(
        ["swiftc", str(main), "-o", str(binary)], capture_output=True, text=True
    )
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
