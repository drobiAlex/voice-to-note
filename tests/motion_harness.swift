// Compiled with the vendored solver and the app's PuckMotion constants by pytest.

/// AppKit supplies this convenience in production; the Foundation-only solver harness does not.
extension CGPoint {
    static var zero: CGPoint { CGPoint(x: 0, y: 0) }
}

/// The same fixed-step accumulator checks observed frame times, not frame counts.
func settle(at rate: Double, distance: Double) -> Double {
    let physics = SpringMotionPhysics(configuration: PuckMotion.configuration,
                                      timeStep: Float(PuckMotion.step))
    let destination = CGPoint(x: distance, y: distance)
    var state = SpringMotionState(position: .zero, velocity: .zero)
    var accumulator = 0.0
    var peak = 0.0
    var visiblePeaks = 0
    var priorVelocity = 1.0
    for frame in 1...240 {
        accumulator += 1 / rate
        while accumulator >= PuckMotion.step {
            state = physics.calculateNextState(from: state, destinationPoint: destination)
            accumulator -= PuckMotion.step
            peak = max(peak, state.position.x - distance)
            if priorVelocity > 0 && state.velocity.horizontal <= 0 && state.position.x - distance >= 0.5 {
                visiblePeaks += 1
            }
            priorVelocity = state.velocity.horizontal
        }
        if abs(state.velocity.horizontal) < 0.001 && abs(state.velocity.vertical) < 0.001
            && abs(state.position.x - destination.x) < 0.5
            && abs(state.position.y - destination.y) < 0.5 {
            let seconds = Double(frame) / rate
            precondition(seconds <= 0.7, "Motion missed the 700 ms deadline")
            precondition(peak >= 4 && peak <= 12, "Overshoot must be 4–12 pt")
            precondition(visiblePeaks == 1, "There must be only one visible bounce")
            print("\(Int(rate)) Hz, \(distance) pt: \(seconds) s, overshoot \(peak) pt")
            return seconds
        }
    }
    fatalError("Spring never settled")
}

for distance in [180.0, 184.0, 200.0] {
    let sixty = settle(at: 60, distance: distance)
    let oneTwenty = settle(at: 120, distance: distance)
    precondition(abs(sixty - oneTwenty) / oneTwenty <= 0.05, "Refresh rates disagree")
}

/// Compare the visible contour, not just the window travel: old tabs used raw
/// travel while the new contour begins at contact. Both profiles advance the
/// same production-sized 184-point tuck at the fixed physics timestep.
func profile(omega: Float, amount: @escaping (CGFloat) -> CGFloat) -> (morph: Double, settle: Double) {
    let physics = SpringMotionPhysics(
        configuration: SpringConfiguration(angularFrequency: omega, dampingRatio: PuckMotion.dampingRatio),
        timeStep: Float(PuckMotion.step)
    )
    let destination = CGPoint(x: 184, y: 0)
    var state = SpringMotionState(position: .zero, velocity: .zero)
    var tenPercent: Double?
    var ninetyPercent: Double?
    for step in 1...240 {
        state = physics.calculateNextState(from: state, destinationPoint: destination)
        let visible = amount(min(max(state.position.x / destination.x, 0), 1))
        let seconds = Double(step) * PuckMotion.step
        if visible >= 0.1, tenPercent == nil { tenPercent = seconds }
        if visible >= 0.9, ninetyPercent == nil { ninetyPercent = seconds }
        if let tenPercent, let ninetyPercent,
           abs(state.velocity.horizontal) < 0.001 && abs(state.position.x - destination.x) < 0.5 {
            return (ninetyPercent - tenPercent, seconds)
        }
    }
    fatalError("Profile never settled")
}
let oldProfile = profile(omega: 40) { $0 }
let newProfile = profile(omega: PuckMotion.angularFrequency) { PuckEdgeContour.contactAmount(for: $0) }
precondition(newProfile.morph > oldProfile.morph, "Visible morph became faster")
print("Visible morph 10–90%: old \(oldProfile.morph) s, new \(newProfile.morph) s; fixed-step settle: old \(oldProfile.settle) s, new \(newProfile.settle) s")

/// Redirection feeds the incoming velocity into the actual solver, preserving
/// momentum instead of resetting it when the destination changes.
let physics = SpringMotionPhysics(configuration: PuckMotion.configuration,
                                  timeStep: Float(PuckMotion.step))
var moving = SpringMotionState(position: .zero, velocity: .zero)
for _ in 0..<5 {
    moving = physics.calculateNextState(from: moving, destinationPoint: CGPoint(x: 184, y: 0))
}
let redirected = physics.calculateNextState(from: moving, destinationPoint: .zero)
let reset = physics.calculateNextState(
    from: SpringMotionState(position: moving.position, velocity: .zero), destinationPoint: .zero)
precondition(redirected.position.x > reset.position.x, "Redirect lost incoming momentum")
print("Redirect retains incoming velocity \(moving.velocity.horizontal) pt/s")

/// Both signs on both axes cover the four screen edges, including reversals
/// carrying velocity toward the off-screen boundary when a new target arrives.
for axis in 0...1 {
    for sign in [-1.0, 1.0] {
        let outward: (CGPoint) -> Double = { (axis == 0 ? $0.x : $0.y) * sign }
        let bound: (CGPoint) -> CGPoint = { point in
            var safe = point
            if axis == 0 { safe.x = min(point.x * sign, 195) * sign }
            else { safe.y = min(point.y * sign, 195) * sign }
            return safe
        }
        let requested = CGPoint(x: axis == 0 ? 184 * sign : 0,
                                y: axis == 1 ? 184 * sign : 0)
        var state = SpringMotionState(
            position: CGPoint(x: axis == 0 ? 175 * sign : 0, y: axis == 1 ? 175 * sign : 0),
            velocity: .init(horizontal: axis == 0 ? 1800 * sign : 0,
                            vertical: axis == 1 ? 1800 * sign : 0))
        let destination = PuckMotion.safeDestination(requested, from: state, physics: physics, bound: bound)
        precondition(outward(destination) < outward(requested), "Unsafe target was not adjusted")
        for _ in 0..<120 {
            state = physics.calculateNextState(from: state, destinationPoint: destination)
            precondition(outward(state.position) <= 195.001, "Prediction concealed the sliver")
        }
    }
}
print("Four edge predictions preserve at least half the peek with incoming velocity")

/// The edge contour is Foundation-only production geometry. These checks make
/// the tab's actual joints and display anchors executable without AppKit.
func near(_ left: CGFloat, _ right: CGFloat, _ tolerance: CGFloat = 0.001) -> Bool {
    abs(left - right) <= tolerance
}
func finite(_ point: CGPoint) -> Bool { point.x.isFinite && point.y.isFinite }
func same(_ left: PuckEdgeContour.Cubic, _ right: PuckEdgeContour.Cubic) -> Bool {
    near(left.start.x, right.start.x) && near(left.start.y, right.start.y)
        && near(left.control1.x, right.control1.x) && near(left.control1.y, right.control1.y)
        && near(left.control2.x, right.control2.x) && near(left.control2.y, right.control2.y)
        && near(left.end.x, right.end.x) && near(left.end.y, right.end.y)
}
precondition(PuckEdgeContour.contactAmount(for: 0) == 0, "Tab starts before contact")
precondition(PuckEdgeContour.contactAmount(for: 0.5) < 0.15, "Approach stopped reading as a circle")
precondition(PuckEdgeContour.contactAmount(for: 1) == 1, "Tab never reaches its terminal outline")
for amount in [CGFloat(0), 0.5, 1] {
    for boundary in [CGFloat(11), 22, 35] {
        let contour = PuckEdgeContour.segments(amount: amount, boundary: boundary)
        precondition(contour.count == 8, "Contour topology changed")
        for index in contour.indices {
            let segment = contour[index]
            let next = contour[(index + 1) % contour.count]
            precondition(finite(segment.start) && finite(segment.control1)
                && finite(segment.control2) && finite(segment.end), "Contour contains a nonfinite point")
            precondition(near(segment.end.x, next.start.x) && near(segment.end.y, next.start.y),
                         "Contour is not closed")
        }
        if amount == 1 {
            precondition(near(contour[0].start.x, boundary) && near(contour[4].start.x, boundary),
                         "Tab shoulders missed the moving screen boundary")
            for index in contour.indices {
                let prior = contour[(index + 7) % 8]
                let current = contour[index]
                let incoming = CGPoint(x: current.start.x - prior.control2.x,
                                       y: current.start.y - prior.control2.y)
                let outgoing = CGPoint(x: current.control1.x - current.start.x,
                                       y: current.control1.y - current.start.y)
                precondition(near(incoming.x, outgoing.x) && near(incoming.y, outgoing.y),
                             "Terminal contour has a cusp")
            }
        }
        for (upper, lower) in [(0, 3), (1, 2), (4, 7), (5, 6)] {
            let top = contour[upper]
            let bottom = contour[lower]
            precondition(near(top.start.x, bottom.end.x) && near(top.start.y, -bottom.end.y),
                         "Contour lost its vertical symmetry")
            precondition(near(top.control1.x, bottom.control2.x) && near(top.control1.y, -bottom.control2.y),
                         "Contour controls lost their vertical symmetry")
            precondition(near(top.control2.x, bottom.control1.x) && near(top.control2.y, -bottom.control1.y),
                         "Contour controls lost their vertical symmetry")
        }
    }
}
print("Edge contour is finite, symmetric, closed, tangent-continuous, and boundary-anchored")

let circularRim = PuckEdgeContour.approximateLength(amount: 0, boundary: 0)
precondition(abs(circularRim - 2 * .pi * 99.5) < 1, "Circle rim length is not sampled accurately")
for boundary in [CGFloat(11), 22, 35] {
    let tabRim = PuckEdgeContour.approximateLength(amount: 1, boundary: boundary)
    precondition(tabRim.isFinite && tabRim > 36, "Tab cannot carry a single rim dash")
}
print("Contour-length dash period keeps one continuous processing highlight")

/// A held drag reaches the same clipped tab without moving its window.  The
/// translated terminal remains finite at the real 202-point boundary, where
/// the ordinary tucked contour is intentionally not shown in full.
for amount in [CGFloat(0), 0.29, 0.57, 0.86, 1] {
    let held = PuckEdgeContour.heldSegments(amount: amount, boundary: 202)
    precondition(held.count == 8, "Held contour changed topology")
    for index in held.indices {
        let next = held[(index + 1) % held.count]
        precondition(finite(held[index].start) && finite(held[index].control1)
            && finite(held[index].control2) && finite(held[index].end), "Held contour contains a nonfinite point")
        precondition(near(held[index].end.x, next.start.x) && near(held[index].end.y, next.start.y),
                     "Held contour is not closed")
    }
    for (upper, lower) in [(0, 3), (1, 2), (4, 7), (5, 6)] {
        precondition(near(held[upper].start.x, held[lower].end.x)
            && near(held[upper].start.y, -held[lower].end.y), "Held contour lost symmetry")
    }
}
let heldCircle = PuckEdgeContour.heldSegments(amount: 0, boundary: 202)
let circle = PuckEdgeContour.segments(amount: 0, boundary: 0)
precondition(zip(heldCircle, circle).allSatisfy { same($0.0, $0.1) },
             "The proximity band does not begin as a circle")
let held = PuckEdgeContour.heldSegments(amount: 1, boundary: 202)
precondition(near(held[0].start.x, 202) && near(held[4].start.x, 202),
             "The held tab missed the live display boundary")
for index in held.indices {
    let prior = held[(index + 7) % 8]
    let current = held[index]
    precondition(near(current.start.x - prior.control2.x, current.control1.x - current.start.x)
        && near(current.start.y - prior.control2.y, current.control1.y - current.start.y),
        "The held terminal contour has a cusp")
}
let heldAmount: CGFloat = 0.57
let heldAtRelease = PuckEdgeContour.heldSegments(amount: heldAmount, boundary: 202)
let releaseStart = PuckEdgeContour.releaseSegments(
    heldAmount: heldAmount, tuckedAmount: heldAmount, boundary: 202, transition: 0)
let tuckedAtEnd = PuckEdgeContour.segments(amount: 1, boundary: 22)
let releaseEnd = PuckEdgeContour.releaseSegments(
    heldAmount: heldAmount, tuckedAmount: 1, boundary: 22, transition: 1)
precondition(zip(releaseStart, heldAtRelease).allSatisfy { same($0.0, $0.1) },
             "Release changed the held outline on its first frame")
precondition(zip(releaseEnd, tuckedAtEnd).allSatisfy { same($0.0, $0.1) },
             "Release did not finish on the ordinary tucked outline")
let heldRim = PuckEdgeContour.approximateLength(of: heldAtRelease)
let releaseRim = PuckEdgeContour.approximateLength(of: PuckEdgeContour.releaseSegments(
    heldAmount: heldAmount, tuckedAmount: 0.8, boundary: 80, transition: 0.4))
precondition(heldRim.isFinite && releaseRim.isFinite && heldRim > 36 && releaseRim > 36,
             "The actual held or release path cannot carry its working-rim dash")
print("Held and release contours share endpoints and measure their actual rim paths")

precondition(PuckEdgeProximity.amount(for: 24) == 0, "Live morph starts outside its window margin")
precondition(PuckEdgeProximity.amount(for: 2) == 1, "Live morph misses its terminal tab")
precondition(near(PuckEdgeProximity.amount(for: 13), 0.5), "Live morph is not continuous across its band")
precondition(PuckEdgeProximity.chosenIndex(
    distances: [12, 10, 90, 90], exposed: [true, true, true, true], current: 0) == 0,
    "Corner jitter changed the retained edge")
precondition(PuckEdgeProximity.chosenIndex(
    distances: [20, 10, 90, 90], exposed: [true, true, true, true], current: 0) == 1,
    "A decisively closer corner edge was not selected")
precondition(PuckEdgeProximity.chosenIndex(
    distances: [2, 90, 90, 90], exposed: [false, true, true, true], current: nil) == nil,
    "A shared display seam accepted a live morph")
precondition(PuckEdgeProximity.chosenIndex(
    distances: [2, 18, 90, 90], exposed: [false, true, true, true], current: 0) == 1,
    "An exposed outer edge lost to a nearer shared seam")
precondition(PuckEdgeProximity.chosenIndex(
    distances: [25, 27, 90, 90], exposed: [false, true, true, true], current: nil, maximumGap: 28) == 1,
    "Release selected a nearer shared seam instead of an exposed edge")
let releaseOrigin = CGPoint(x: 100, y: 20)
let releaseTarget = CGPoint(x: 200, y: 20)
precondition(PuckEdgeProximity.transition(from: releaseOrigin, to: releaseTarget, at: releaseOrigin) == 0,
             "A re-grab cannot reverse from the exact held frame")
precondition(near(PuckEdgeProximity.transition(
    from: releaseOrigin, to: releaseTarget, at: CGPoint(x: 150, y: 75)), 0.5),
    "Release progress changed with motion along the edge")
print("Proximity strength, corner hysteresis, seam filtering, and release reversal are stable")
