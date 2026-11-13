// Appended after motion_harness.swift by tests/test_motion.py, so it shares that file's compile
// with the vendored solver and the app's PuckMotion, PuckEdgeContour and PuckDockModel source.
//
// Everything here runs only for the sections the caller names on the command line, so each
// behaviour is its own Python test while one compile serves all of them.

let requestedSections = Set(CommandLine.arguments.dropFirst())
func wants(_ section: String) -> Bool { requestedSections.contains(section) }

typealias Dock = PuckDockModel
let panelSize = CGSize(width: 248, height: 248)
let panelInset: CGFloat = 24
let bodyWidth: CGFloat = 200

/// SplitMix64, carried here because the app's own copy lives beside code that needs AppKit.
struct TestRandom {
    var state: UInt64
    init(_ seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9e37_79b9_7f4a_7c15
        var z = state
        z = (z ^ (z >> 30)) &* 0xbf58_476d_1ce4_e5b9
        z = (z ^ (z >> 27)) &* 0x94d0_49bb_1331_11eb
        return z ^ (z >> 31)
    }
    mutating func unit() -> CGFloat { CGFloat(next() >> 11) / CGFloat(UInt64(1) << 53) }
    mutating func int(_ count: Int) -> Int { Int(next() % UInt64(count)) }
    mutating func between(_ low: CGFloat, _ high: CGFloat) -> CGFloat { low + (high - low) * unit() }
    mutating func pick<T>(_ values: [T]) -> T { values[int(values.count)] }
}

func screen(_ width: CGFloat = 1440, _ height: CGFloat = 875) -> Dock.Screen {
    Dock.Screen(visibleFrame: CGRect(x: 0, y: 0, width: width, height: height))
}

func makeModel(_ place: Dock.Place, on current: Dock.Screen = screen(),
               using solver: SpringMotionPhysics? = nil) -> Dock {
    var model: Dock
    if let solver {
        model = Dock(panelSize: panelSize, inset: panelInset, screen: current, physics: solver)
    } else {
        model = Dock(panelSize: panelSize, inset: panelInset, screen: current)
    }
    model.send(.restore(place))
    return model
}

/// A puck that has finished peeking out of its edge, with the pointer left on it.
func makePeeking(_ edge: Dock.Edge, along: CGFloat, on current: Dock.Screen = screen()) -> Dock {
    var model = makeModel(.tucked(edge, along: along), on: current)
    model.send(.reveal)
    let origin = model.revealedOrigin(edge, along: along)
    model.send(.pointer(CGPoint(x: origin.x + panelSize.width / 2, y: origin.y + panelSize.height / 2)))
    run(&model, until: { $0.isAtRest }, limit: 2, "never finished peeking")
    return model
}

func kind(_ pose: Dock.Pose) -> String {
    switch pose {
    case .hanging: return "hanging"
    case .free: return "free"
    case .held: return "held"
    case .tucked: return "tucked"
    case .peeking: return "peeking"
    }
}

/// The edge, along and whether it is tucked, for an edge pose; nil otherwise.
func edgePose(_ model: Dock) -> (edge: Dock.Edge, along: CGFloat, tucked: Bool)? {
    switch model.pose {
    case let .tucked(edge, along): return (edge, along, true)
    case let .peeking(edge, along): return (edge, along, false)
    default: return nil
    }
}

func isTucked(_ model: Dock) -> Bool { edgePose(model)?.tucked == true }
func isPeeking(_ model: Dock) -> Bool { edgePose(model)?.tucked == false }

/// A pointer over the middle of the puck's body, which is where a hand finds it.
func centre(_ model: Dock) -> CGPoint {
    CGPoint(x: model.position.x + panelSize.width / 2, y: model.position.y + panelSize.height / 2)
}

/// The invariants that hold in every frame, not only at rest.
func checkFrame(_ model: Dock, _ context: String) {
    let shown = model.presentation
    precondition(shown.origin.x.isFinite && shown.origin.y.isFinite, "origin not finite \(context)")
    precondition(shown.amount >= 0 && shown.amount <= 1, "amount \(shown.amount) out of range \(context)")
    precondition(shown.alpha == Dock.alpha(for: shown.amount), "alpha is not a function of amount \(context)")
    precondition(shown.controlsOpacity == Dock.controlsOpacity(for: shown.amount),
                 "controls are not a function of amount \(context)")
    precondition(shown.alpha >= PuckMotion.tuckedAlpha && shown.alpha <= 1, "alpha out of range \(context)")
    if shown.edge == nil { precondition(shown.amount == 0, "merged with no edge \(context)") }
}

/// The legal resting poses, and what each must look like to the last bit.
func checkRest(_ model: Dock, _ context: String) {
    precondition(model.isAtRest, "not at rest \(context)")
    let shown = model.presentation
    precondition(model.velocity.isZero, "resting with velocity \(context)")
    precondition(shown.release == nil, "resting mid release \(context)")
    precondition(shown.origin == model.target, "rest origin is not the target \(context)")
    switch model.pose {
    case .held:
        preconditionFailure("held is not a resting pose \(context)")
    case .hanging, .free:
        precondition(shown.amount == 0 && shown.alpha == 1 && shown.edge == nil && shown.controlsOpacity == 1,
                     "free pose is merged \(context)")
    case let .tucked(edge, along):
        precondition(shown.origin == model.tuckedOrigin(edge, along: along), "tucked off its frame \(context)")
        precondition(shown.amount == 1, "tucked but amount \(shown.amount) \(context)")
        precondition(shown.alpha == PuckMotion.tuckedAlpha && shown.controlsOpacity == 0, "tucked look \(context)")
    case let .peeking(edge, along):
        precondition(shown.origin == model.revealedOrigin(edge, along: along), "peeking off its frame \(context)")
        precondition(shown.amount == 0 && shown.alpha == 1 && shown.controlsOpacity == 1, "peek look \(context)")
    }
}

func run(_ model: inout Dock, for seconds: Double, step: Double = 1 / 60, context: String = "",
         each: ((Dock) -> Void)? = nil) {
    var elapsed = 0.0
    while elapsed < seconds {
        model.advance(by: step)
        elapsed += step
        checkFrame(model, context)
        each?(model)
    }
}

/// Run until a condition holds, failing if it never does.
func run(_ model: inout Dock, until done: (Dock) -> Bool, limit: Double, _ message: String) {
    var elapsed = 0.0
    while !done(model) {
        precondition(elapsed < limit, message)
        model.advance(by: 1 / 120)
        elapsed += 1 / 120
        checkFrame(model, message)
    }
}

/// A hand takes the puck from where it is, carries it so its body is `gap` from the edge, and lets go.
func dragAndRelease(_ model: inout Dock, toward edge: Dock.Edge, gap: CGFloat, along: CGFloat,
                    steps: Int = 6, releasing: Bool = true) {
    let area = model.screen.visibleFrame
    var goal = CGPoint(x: 0, y: 0)
    switch edge {
    case .left: goal = CGPoint(x: area.minX + gap - panelInset, y: area.minY + along * area.height - 124)
    case .right: goal = CGPoint(x: area.maxX - gap - bodyWidth - panelInset, y: area.minY + along * area.height - 124)
    case .top: goal = CGPoint(x: area.minX + along * area.width - 124, y: area.maxY - gap - bodyWidth - panelInset)
    case .bottom: goal = CGPoint(x: area.minX + along * area.width - 124, y: area.minY + gap - panelInset)
    }
    let grip = CGPoint(x: 120, y: 130)
    let from = model.position
    model.send(.pressed)
    model.send(.dragBegan(CGPoint(x: from.x + grip.x, y: from.y + grip.y)))
    for step in 1...steps {
        let t = CGFloat(step) / CGFloat(steps)
        model.send(.dragged(CGPoint(x: from.x + (goal.x - from.x) * t + grip.x,
                                    y: from.y + (goal.y - from.y) * t + grip.y)))
        model.advance(by: 1 / 60)
    }
    if releasing { model.send(.released(CGPoint(x: goal.x + grip.x, y: goal.y + grip.y))) }
}

// MARK: property test

/// Random but replayable event sequences, ended by letting go of whatever is
/// held and then only letting time pass: wherever the model comes to rest must
/// be one of the legal poses, to the last bit, and must stay there.
func propertyRun(seed: UInt64, stats: inout [String: Int]) {
    var rng = TestRandom(seed)
    let widths: [CGFloat] = [1440, 1920, 900]
    let heights: [CGFloat] = [875, 1055, 700]
    var current = screen(rng.pick(widths), rng.pick(heights))
    var model = makeModel(.free(CGPoint(x: 400, y: 300)), on: current)
    switch rng.int(5) {
    case 1: model.send(.restore(.tucked(rng.pick(Dock.Edge.allCases), along: rng.unit())))
    case 2: model.send(.restore(.hanging(CGPoint(x: 700, y: 400))))
    case 3: model.send(.restore(.free(CGPoint(x: rng.between(-50, 1300), y: rng.between(-50, 700)))))
    default: break
    }
    let context = "seed \(seed)"
    let gaps: [CGFloat] = [0, 1, 2, 10, 23, 24, 25, 26, 27, 28, 29, 35, 90]
    func interval(_ rng: inout TestRandom) -> Double {
        switch rng.int(20) {
        case 0: return 0.3 + Double(rng.unit()) * 1.7
        case 1...8: return 1 / 120
        default: return 1 / 60
        }
    }
    func somePointer(_ rng: inout TestRandom, _ model: Dock) -> CGPoint? {
        let area = model.screen.visibleFrame
        switch rng.int(8) {
        case 0: return nil
        case 1: return CGPoint(x: rng.between(area.minX, area.maxX), y: rng.between(area.minY, area.maxY))
        case 2: return centre(model)
        default:
            let edge = edgePose(model)?.edge ?? rng.pick(Dock.Edge.allCases)
            let along = edgePose(model)?.along ?? rng.unit()
            let rect = model.tabFootprint(edge, along: along)
            switch rng.int(5) {
            case 0: return CGPoint(x: rect.minX, y: rect.minY)
            case 1: return CGPoint(x: rect.maxX, y: rect.maxY)
            case 2: return CGPoint(x: rect.midX, y: rect.minY)
            case 3: return CGPoint(x: rect.minX, y: rect.maxY)
            default: return CGPoint(x: rng.between(rect.minX, rect.maxX), y: rng.between(rect.minY, rect.maxY))
            }
        }
    }
    for _ in 0..<(15 + rng.int(40)) {
        switch rng.int(14) {
        case 0, 1, 2:
            for _ in 0...rng.int(6) {
                model.advance(by: interval(&rng))
                checkFrame(model, context)
            }
        case 3, 4:
            dragAndRelease(&model, toward: rng.pick(Dock.Edge.allCases), gap: rng.pick(gaps),
                           along: rng.unit(), steps: 1 + rng.int(8), releasing: rng.int(6) != 0)
        case 5:
            model.send(.pointer(centre(model)))
            model.send(.pressed)
            if rng.int(2) == 0 { model.advance(by: interval(&rng)) }
            model.send(.clicked)
        case 6, 7, 8:
            model.send(.pointer(somePointer(&rng, model)))
        case 9:
            model.send(.reveal)
        case 10:
            model.send(.pressed)
            for _ in 0...rng.int(5) { model.advance(by: interval(&rng)) }
            if rng.int(2) == 0 { model.send(.released(centre(model))) } else { model.send(.clicked) }
        case 11:
            if rng.int(4) == 0 {
                current = screen(rng.pick(widths), rng.pick(heights))
                model.send(.screenChanged(current))
            }
        case 12:
            if rng.int(5) == 0 { model.send(.reduceMotion(rng.int(2) == 0)) }
        default:
            model.send(.pointer(nil))
        }
        checkFrame(model, context)
    }
    model.send(.released(centre(model)))
    model.send(.reduceMotion(false))
    stats["notes", default: 0] += model.drainNotes().count
    // Nothing but time from here: the hover chain is at most a dwell, a peek, a
    // grace and a tuck, each inside the deadline, and then it must stay put.
    let horizon = PuckMotion.dwell + PuckMotion.grace + PuckMotion.deadline * 3 + 0.5
    run(&model, for: horizon, context: context)
    checkRest(model, context)
    if let shown = edgePose(model), !shown.tucked {
        var keeps = false
        if let point = model.pointer { keeps = model.keepsPeeking(point, shown.edge, along: shown.along) }
        precondition(keeps, "peeking with the pointer elsewhere never tucked \(context)")
    }
    let before = model.pose
    run(&model, for: 3, context: context)
    precondition(model.pose == before, "pose changed with no events \(context)")
    checkRest(model, context)
    stats[kind(model.pose), default: 0] += 1
}

if wants("property") {
    var stats: [String: Int] = [:]
    let runs: UInt64 = 6000
    for seed in 1...runs { propertyRun(seed: seed, stats: &stats) }
    // A property test that only ever sees one kind of ending proves little.
    for ending in ["free", "tucked", "peeking"] {
        precondition(stats[ending, default: 0] >= 100, "the property runs rarely ended \(ending): \(stats)")
    }
    precondition(stats["notes", default: 0] > 1000, "the property runs said too little: \(stats)")
    print("property: \(runs) seeded runs (seeds 1...\(runs)), rested as \(stats.sorted { $0.key < $1.key })")
}

// MARK: directed tests

if wants("arrival") {
    // A velocity a hand could never give is handed to the spring right after the
    // tuck is decided: whatever it does, it must end on the tucked frame and
    // never cost the sliver.
    for edge in Dock.Edge.allCases {
        var model = makePeeking(edge, along: 0.4)
        model.send(.pointer(nil))
        run(&model, until: { isTucked($0) }, limit: 2, "never tucked on \(edge)")
        let outward = 6000.0
        switch edge {
        case .right: model.velocity = .init(horizontal: outward, vertical: 0)
        case .left: model.velocity = .init(horizontal: -outward, vertical: 0)
        case .top: model.velocity = .init(horizontal: 0, vertical: outward)
        case .bottom: model.velocity = .init(horizontal: 0, vertical: -outward)
        }
        let area = model.screen.visibleFrame
        var worst = CGFloat.infinity
        run(&model, for: 3, context: "arrival \(edge)") { current in
            let body = CGRect(x: current.position.x + panelInset, y: current.position.y + panelInset,
                              width: bodyWidth, height: bodyWidth)
            let seen = body.intersection(area)
            var depth: CGFloat = 0
            if !seen.isNull {
                depth = (edge == .left || edge == .right) ? seen.width : seen.height
            }
            worst = min(worst, depth)
        }
        precondition(worst >= PuckMotion.peek / 2 - 0.001, "arrival lost its sliver on \(edge): \(worst)")
        checkRest(model, "arrival \(edge)")
        precondition(model.presentation.amount == 1, "arrival rested short of the tab on \(edge)")
    }
    print("arrival: four edges end exactly tucked, sliver never below half the peek")
}

if wants("reveal") {
    for edge in Dock.Edge.allCases {
        var model = makeModel(.tucked(edge, along: 0.3))
        model.send(.pointer(nil))
        model.send(.reveal)
        precondition(isPeeking(model), "reveal did not peek")
        var elapsed = 0.0
        var tuckedAt: Double?
        while elapsed < 4 {
            model.advance(by: 1 / 60)
            elapsed += 1 / 60
            if tuckedAt == nil, isTucked(model) { tuckedAt = elapsed }
        }
        precondition(tuckedAt != nil, "a reveal with no pointer never tucked again")
        precondition(tuckedAt! <= PuckMotion.grace + 0.05, "tucked after \(tuckedAt!) s, later than the grace")
        checkRest(model, "reveal \(edge)")
    }
    print("reveal: a peek nobody is hovering tucks after the grace on every edge")
}

if wants("tab_end") {
    for edge in Dock.Edge.allCases {
        for end in 0..<4 {
            for onTheEdge in [true, false] {
                var model = makeModel(.tucked(edge, along: 0.5))
                let rect = model.tabFootprint(edge, along: 0.5)
                var point = CGPoint(x: (end % 2 == 0) ? rect.minX : rect.maxX,
                                    y: (end / 2 == 0) ? rect.minY : rect.maxY)
                if !onTheEdge {
                    point.x += (end % 2 == 0) ? 1 : -1
                    point.y += (end / 2 == 0) ? 1 : -1
                }
                model.send(.pointer(point))
                var flips = 0
                var last = kind(model.pose)
                run(&model, for: 10, context: "tab end \(edge) \(end)") { current in
                    if kind(current.pose) != last { flips += 1; last = kind(current.pose) }
                }
                precondition(flips <= 1, "pointer parked on a tab end flipped \(flips) times on \(edge) end \(end)")
                checkRest(model, "tab end")
            }
        }
    }
    print("tab_end: a pointer parked on any tab end peeks once and stays")
}

if wants("quick_grab") {
    var model = makePeeking(.right, along: 0.5)
    model.send(.pointer(nil))
    run(&model, until: { isTucked($0) }, limit: 2, "never tucked")
    run(&model, for: 0.1)
    precondition(!model.isAtRest, "the grab was meant to catch the tuck in flight")
    model.send(.pressed)
    let grip = centre(model)
    model.send(.dragBegan(grip))
    for step in 1...10 {
        let t = CGFloat(step) / 10
        model.send(.dragged(CGPoint(x: grip.x + (600 - grip.x) * t, y: grip.y + (400 - grip.y) * t)))
        model.advance(by: 1 / 60)
    }
    model.send(.released(CGPoint(x: 600, y: 400)))
    precondition(model.pose == .free, "a puck dragged to the middle of the screen is not free")
    let shown = model.presentation
    precondition(shown.alpha == 1 && shown.amount == 0 && shown.edge == nil,
                 "a puck grabbed during a tuck came out dim or merged: alpha \(shown.alpha) amount \(shown.amount)")
    run(&model, for: 1)
    checkRest(model, "quick grab")
    print("quick_grab: a drag begun inside the tuck's slide ends free with alpha exactly 1")
}

if wants("press_mid_spring") {
    for release in 0..<3 {
        var model = makeModel(.tucked(.left, along: 0.6))
        model.send(.pointer(nil))
        model.send(.reveal)
        run(&model, for: 0.1)
        let frozen = model.position
        model.send(.pressed)
        run(&model, for: 1.0)
        precondition(model.position == frozen, "a pressed puck kept moving")
        precondition(!model.isAtRest, "a pressed puck mid-spring cannot be at rest")
        switch release {
        case 0:
            model.send(.clicked)
        case 1:
            model.send(.released(centre(model)))
        default:
            model.send(.dragBegan(centre(model)))
            model.send(.released(centre(model)))
        }
        run(&model, for: 4)
        checkRest(model, "press mid spring \(release)")
    }
    var lost = makeModel(.tucked(.top, along: 0.5))
    lost.send(.reveal)
    run(&lost, for: 0.05)
    lost.send(.pressed)
    run(&lost, for: PuckMotion.pressLimit + 4)
    checkRest(lost, "a lost mouse-up")
    print("press_mid_spring: a press frozen mid-flight settles after click, release, drag or a lost mouse-up")
}

if wants("deadline") {
    let undamped = SpringMotionPhysics(
        configuration: SpringConfiguration(angularFrequency: PuckMotion.angularFrequency, dampingRatio: 0),
        timeStep: Float(PuckMotion.step))
    var model = makeModel(.tucked(.right, along: 0.5), using: undamped)
    model.send(.reveal)
    let origin = model.revealedOrigin(.right, along: 0.5)
    model.send(.pointer(CGPoint(x: origin.x + panelSize.width / 2, y: origin.y + panelSize.height / 2)))
    var elapsed = 0.0
    var lastMoving = 0.0
    while !model.isAtRest {
        model.advance(by: 1 / 60)
        elapsed += 1 / 60
        if !model.isAtRest { lastMoving = elapsed }
        precondition(elapsed <= PuckMotion.deadline + 0.05, "a spring that cannot converge outlived the deadline")
    }
    precondition(lastMoving > PuckMotion.nominalSettle, "the undamped spring should still be moving at the nominal time")
    precondition(elapsed >= PuckMotion.deadline - 0.05, "snapped before the deadline")
    checkRest(model, "deadline")
    print("deadline: an unconverging spring is snapped to rest at \(elapsed) s")
}

if wants("retarget") {
    var model = makePeeking(.right, along: 0.5)
    model.send(.pointer(nil))
    run(&model, until: { isTucked($0) }, limit: 2, "never tucked")
    run(&model, for: 0.06)
    let moving = model.velocity.horizontal
    let place = model.position
    precondition(abs(moving) > 10, "the tuck should still be moving")
    model.send(.reveal)
    precondition(model.velocity.horizontal == moving && model.position == place,
                 "retargeting lost position or velocity")
    let origin = model.revealedOrigin(.right, along: 0.5)
    model.send(.pointer(CGPoint(x: origin.x + panelSize.width / 2, y: origin.y + panelSize.height / 2)))
    run(&model, for: 3)
    checkRest(model, "retarget")
    print("retarget: reversing a tuck mid-flight keeps position and velocity and ends exactly revealed")
}

if wants("release") {
    let cases: [(gap: CGFloat, tucks: Bool)] = [
        (0, true), (10, true), (24, true), (26, true), (28, true), (28.5, false), (40, false),
    ]
    for (gap, tucks) in cases {
        var model = makeModel(.free(CGPoint(x: 500, y: 300)))
        dragAndRelease(&model, toward: .right, gap: gap, along: 0.5)
        precondition(isTucked(model) == tucks, "release at gap \(gap) should tuck: \(tucks)")
        let heard = model.drainNotes()
        precondition(heard.contains(.placed(model.place)), "release said nothing about where it was left")
        run(&model, for: 3)
        checkRest(model, "release \(gap)")
    }
    // Continuity: what the hand showed is what the first frame after release shows.
    var model = makeModel(.free(CGPoint(x: 500, y: 300)))
    dragAndRelease(&model, toward: .left, gap: 10, along: 0.5, releasing: false)
    let shownByHand = model.presentation
    precondition(shownByHand.held && shownByHand.edge == .left && shownByHand.amount > 0.5 && shownByHand.amount < 1,
                 "held outline missing")
    model.send(.released(centre(model)))
    let afterRelease = model.presentation
    precondition(abs(afterRelease.amount - shownByHand.amount) < 1e-9 && afterRelease.release != nil,
                 "release changed the outline")
    run(&model, for: 3)
    checkRest(model, "release continuity")
    print("release: the magnet takes a body up to 28 pt, the outline continues, and rest is exact")
}

if wants("quiet_modes") {
    var model = makeModel(.tucked(.bottom, along: 0.5))
    model.send(.reduceMotion(true))
    model.send(.reveal)
    precondition(model.isAtRest && isPeeking(model), "reduce motion did not apply the end pose")
    checkRest(model, "reduce motion")
    model.send(.pointer(nil))
    model.advance(by: PuckMotion.grace + 0.01)
    precondition(model.isAtRest && isTucked(model), "reduce motion tuck was not immediate")
    checkRest(model, "reduce motion tuck")

    var clicked = makeModel(.tucked(.left, along: 0.5))
    clicked.send(.pointer(centre(clicked)))
    clicked.send(.pressed)
    clicked.send(.clicked)
    precondition(isPeeking(clicked), "a click on a tab did not peek")

    var hanging = makeModel(.hanging(CGPoint(x: 600, y: 300)))
    hanging.send(.dragBegan(CGPoint(x: 700, y: 400)))
    precondition(hanging.drainNotes().isEmpty, "left hanging before it moved")
    hanging.send(.dragged(CGPoint(x: 710, y: 390)))
    precondition(hanging.drainNotes() == [.leftHanging], "the first movement did not leave hanging")
    hanging.send(.released(CGPoint(x: 710, y: 390)))
    precondition(hanging.pose == .free, "a hanging puck carried off is not free")
    print("quiet_modes: reduce motion, click-to-peek and leaving the hanging pose")
}
