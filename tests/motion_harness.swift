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

/// The tab's visible contact phase is measured in the production physics steps,
/// so display-frame quantisation cannot disguise a refresh-rate difference.
func contactDuration() -> Double {
    let physics = SpringMotionPhysics(configuration: PuckMotion.configuration,
                                      timeStep: Float(PuckMotion.step))
    var state = SpringMotionState(position: .zero, velocity: .zero)
    var began: Double?
    for step in 1...240 {
        state = physics.calculateNextState(from: state, destinationPoint: CGPoint(x: 184, y: 0))
        let travel = min(max(state.position.x / 184, 0), 1)
        let seconds = Double(step) * PuckMotion.step
        if travel >= 0.35, began == nil { began = seconds }
        if travel >= 0.99, let began {
            let duration = seconds - began
            precondition(duration >= 0.05 && duration <= 0.15, "Contact merge is not gentle")
            print("Fixed-step contact merge: \(duration) s")
            return duration
        }
    }
    fatalError("Motion never reached the tab")
}
_ = contactDuration()

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
precondition(PuckEdgeContour.contactAmount(for: 0.35) == 0, "Tab starts before contact")
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
