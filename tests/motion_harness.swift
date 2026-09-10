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
