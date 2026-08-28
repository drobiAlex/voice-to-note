// VTN Recorder — the menu bar button a meeting is taped from, for the times a
// call starts and there is no terminal open to type into. It records nothing
// itself: Start runs `vtn record`, the same command a person would run by hand,
// and Stop interrupts it. Everything after that — merging the two tracks,
// transcribing, extracting notes — is that process's own work, and it goes on
// whether this app is still watching or not.

import AppKit
import Foundation
import UserNotifications

/// How far a recording has got. Starting is a state of its own because the tape
/// is not rolling until the recorder says both streams are live, and a red dot
/// shown before then would claim a meeting is being taped that is not.
enum RecorderState {
    case idle
    case starting
    case recording
    case processing
}

/// A device the Mac can record, as the capture helper lists it. The name is
/// carried alongside the UID wherever a choice is remembered, because a UID is
/// a string of hex and says nothing to the person reading the menu.
struct AudioDevice {
    let uid: String
    let name: String
}

/// What this Mac can record from, kept apart by direction. One device is in
/// both lists when it plays and records — a headset does — and the picker for
/// each direction only ever wants its own half.
struct AudioDevices {
    let inputs: [AudioDevice]
    let outputs: [AudioDevice]
}

/// Where this app keeps its things, by the same rule the command line follows:
/// VTN_HOME if it is set, else the macOS application-support directory. An app
/// launched from the Finder inherits no shell, so VTN_HOME is only ever set
/// here for someone who opened it from a terminal.
func vtnHome() -> URL {
    let raw = ProcessInfo.processInfo.environment["VTN_HOME"] ?? ""
    if !raw.isEmpty {
        return URL(fileURLWithPath: raw)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/vtn")
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private lazy var statusItem = NSStatusBar.system.statusItem(
        withLength: NSStatusItem.variableLength
    )
    private lazy var logURL = vtnHome().appendingPathComponent("menubar.log")

    /// The one menu, kept rather than rebuilt, because a recording takes it
    /// away from the status item for its whole length and has to be able to
    /// give the same one back. Its contents are still made fresh on every
    /// opening — that is what `menuNeedsUpdate` is for.
    private let menu = NSMenu()

    /// Whether this launch is a preview rather than a recorder. Settled from
    /// the command line once, because what it decides is not a mode somebody
    /// switches into: a preview drives its own states, feeds its own meters and
    /// starts no recording at all, and an app halfway into one would be an app
    /// taping a meeting nobody asked it to.
    private let previewing = CommandLine.arguments.contains("--preview")

    /// Which state a preview is being held in. It is the one thing a preview
    /// remembers; everything else on screen is the app's own, reached the same
    /// way a recording reaches it.
    private var previewScenario = Scenario.idle

    /// The shimmer belongs to one state and must not outlive it: a timer left
    /// running would go on redrawing a button that has moved on to saying
    /// something else entirely. Hanging it off the state itself is what makes
    /// every way out of processing — finished, failed, quit — stop it too.
    private var state = RecorderState.idle {
        didSet {
            if state == .processing {
                startShimmering()
            } else {
                stopShimmering()
            }
            // a rolling tape is the one state the status item answers a click
            // with a window instead of a menu, and the meters belong to it for
            // the same reason the shimmer does. Emptying them on every state
            // that is not recording is also what makes each recording start
            // from silence rather than from whatever the last one ended on
            if state == .recording {
                // a preview keeps its menu through every state, where a
                // recording gives it up for the panel: that menu is the only
                // way from one scenario to the next, and a status item
                // answering a click with a window instead would strand whoever
                // is looking at it in whichever state they had got to
                if !previewing {
                    openToThePanel()
                }
                showBriefly()
            } else {
                forgetMeters()
                openToTheMenu()
            }
        }
    }
    private var recorder: Process?
    private var errors: Pipe?
    private var unread = ""
    private var startedAt: Date?
    private var ticker: Timer?
    private var shimmer: Timer?
    private var rehearsal: Timer?

    /// The frames the mark is being inked through and which of them is on the
    /// button. Which set it is gets settled when a spell of processing starts,
    /// because Reduce Motion has one of its own — so the count the frame wraps
    /// at is always the count of the set actually being shown.
    private var shimmerFrames = Mark.working
    private var shimmerFrame = 0
    private var mayNotify = false
    private var logFile: FileHandle?
    private var projects: [(name: String, count: Int)]?
    private var devices: AudioDevices?
    private var refreshing = false

    /// How loud each side has been, kept for the length of one recording. The
    /// two of them are the model behind everything the meters draw, and the
    /// state's own didSet is what empties them — a waveform still showing the
    /// last meeting's voices would be describing a tape that stopped.
    private let systemLevels = LevelHistory()
    private let microphoneLevels = LevelHistory()

    /// The floating panel a recording is watched in, and the view inside it.
    /// The two live and die together: the view copies whatever it draws out of
    /// the histories every time it is told to, so there is nothing in it worth
    /// keeping once it is off screen, and building a fresh one on every opening
    /// is also what asks about Reduce Motion again rather than once at launch.
    private var panel: NSPanel?
    private var panelView: RecordingPanelView?

    /// The dial the length of the next recording is set on, and the view in
    /// it. A second floating window rather than a face of the first: the
    /// island is a thing watched during a recording and the dial is a thing
    /// turned before one, and the two are never on screen at once.
    private var dial: NSPanel?
    private var dialView: DialView?

    /// Where the dial lives and how it moves between places; it owns nothing
    /// the two above do not, and goes when they go.
    private var dialDock: DialDock?

    /// How long the recording under way was asked to run, or nothing for one
    /// that runs until it is stopped, and the one timer that stops it. Set the
    /// moment the tape actually rolls rather than when the recorder is
    /// launched, so the length is measured in tape and not in start-up.
    private var timedMinutes: Int?
    private var deadline: Date?
    private var stopAt: Timer?

    /// How long a panel that opened itself stays up: long enough to read the
    /// header and watch both meters move, and over before it is in the way of
    /// anything. What it is for is a moment's confirmation that the tape is
    /// rolling, not a window somebody has been handed to get rid of.
    private static let glance: TimeInterval = 4

    /// How far under the menu bar the panel hangs. The same few points a menu
    /// leaves, and enough for the panel's own shadow to read as a shadow rather
    /// than as a dark seam trapped between two edges.
    private static let hanging: CGFloat = 6

    /// The clock on a panel nobody asked for, which exists only for as long as
    /// such a panel does. Every way the panel closes goes through `closePanel`
    /// and every one of them stops this, which is what keeps a glance that was
    /// cut short from going off later underneath a panel somebody has since
    /// opened for themselves.
    private var glanceOver: Timer?

    /// The two ways a click somewhere else reaches this app while the panel is
    /// open. A borderless panel that never takes focus is never told it lost
    /// any, so nothing else would ever close it.
    private var clicksElsewhere: Any?
    private var clicksHere: Any?

    /// What the button says to a screen reader and to a pointer resting on it,
    /// composed once a second because that is as often as any of it changes.
    private var spoken = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        menu.delegate = self
        // this menu says for itself what can be chosen — its own state decides
        // that. Left to AppKit, a picker whose list has not arrived yet would be
        // greyed out and unopenable, which tells the person nothing at all
        menu.autoenablesItems = false
        openToTheMenu()
        show()
        // a preview asks for nothing and runs nothing: no permission to post
        // notifications about memos it will never make, and no subprocess to
        // fill pickers its own menu does not offer
        if !previewing {
            askToNotify()
            refresh()
        }
    }

    /// A recording in progress is left running rather than waited on: it is its
    /// own process, and the minutes of transcription after the tape stops are
    /// no reason to hold a quit. Stopping the tape first is what makes the
    /// meeting up to this point a memo instead of two half-written files.
    func applicationWillTerminate(_ notification: Notification) {
        stopShimmering()
        if let recorder, recorder.isRunning, state == .starting || state == .recording {
            recorder.interrupt()
        }
    }

    // --- the button in the menu bar -----------------------------------------

    /// Draws the state the recording is actually in. The elapsed time rides on
    /// the button rather than only in the menu, since the whole point of the
    /// thing is being able to see at a glance that a meeting is being taped.
    ///
    /// A red mark and a clock is the whole of what a rolling tape puts up
    /// there. The meters are in the panel: a menu bar is a row of things
    /// glanced at and a meter is a thing watched, and a strip of bars moving in
    /// the corner of the screen buys a reading nobody was looking for at the
    /// price of a menu bar nobody can stop noticing for the length of a
    /// meeting.
    private func show() {
        guard let button = statusItem.button else { return }
        switch state {
        case .idle:
            button.image = Mark.idle
            button.attributedTitle = NSAttributedString(string: "")
        case .starting:
            button.image = Mark.starting
            button.attributedTitle = label("…")
        case .recording:
            speak()
            button.image = Mark.recording
            button.attributedTitle = label(elapsed())
        case .processing:
            button.image = shimmerFrames[shimmerFrame]
            button.attributedTitle = NSAttributedString(string: "")
        }
    }

    /// A fresh reading from each side. These are the only clock the waveforms
    /// have — they arrive ten a second for as long as the tape rolls — which
    /// is why nothing here starts a timer, and why the elapsed time, the
    /// spoken sentence and the panel's footer are left to the one that exists.
    ///
    /// Nothing here touches the button: the mark it is wearing says a meeting
    /// is being taped and says the same thing at every loudness, so there is
    /// nothing for ten readings a second to change about it.
    private func metered(_ system: Double, _ microphone: Double) {
        systemLevels.push(system)
        microphoneLevels.push(microphone)
        panelView?.show(system: systemLevels, microphone: microphoneLevels)
    }

    /// The recording put into a sentence, for a screen reader and for the
    /// pointer resting on the button: how far in it is and what each side is
    /// doing. Once a second, from the ticker, because formatting this ten
    /// times a second would buy nothing that can be read in a tenth of one.
    ///
    /// It is set on the button rather than on the mark the button is wearing.
    /// That mark is one image shared by every drawing of it, and a sentence
    /// written onto it a second at a time would be a sentence about this
    /// recording riding on the picture the next one is drawn from.
    private func speak() {
        let now = Date()
        spoken = "Recording \(elapsed()) — "
            + Meters.spoken("system audio", systemLevels, now: now) + ", "
            + Meters.spoken("microphone", microphoneLevels, now: now)
        statusItem.button?.toolTip = spoken
        statusItem.button?.setAccessibilityLabel(spoken)
        panelView?.showElapsed(elapsed())
        panelView?.showHeading(heading(now: now))
        panelView?.showFooter(system: systemLevels, microphone: microphoneLevels, now: now)
    }

    /// What the island calls the recording: plain "Recording", or with how long
    /// it has left to run when it was given a length. Minutes rather than
    /// seconds — a countdown in seconds is a thing stared at, and the point
    /// of a tape that stops itself is that nobody has to.
    private func heading(now: Date = Date()) -> String {
        guard let deadline else { return "Recording" }
        let left = Int((deadline.timeIntervalSince(now) / 60).rounded(.up))
        if left <= 1 {
            return "Recording · stops in a minute"
        }
        return "Recording · \(left) min left"
    }

    /// Everything the meters were made of, dropped. The panel goes with the
    /// readings rather than being left standing empty: it was built around the
    /// devices this recording used and around a tape that is no longer rolling,
    /// and a floating window still saying "Recording" over a meeting that has
    /// stopped is worse than no window at all.
    private func forgetMeters() {
        systemLevels.reset()
        microphoneLevels.reset()
        closePanel()
        spoken = ""
        statusItem.button?.toolTip = nil
        // back to nothing rather than to some other sentence, which is what
        // hands the describing of the button to the mark it is wearing again
        statusItem.button?.setAccessibilityLabel(nil)
    }

    /// Monospaced digits: proportional ones would shuffle the whole menu bar
    /// sideways every time a second ticks over.
    private func label(_ text: String) -> NSAttributedString {
        let font = NSFont.monospacedDigitSystemFont(
            ofSize: NSFont.smallSystemFontSize, weight: .regular
        )
        return NSAttributedString(string: " " + text, attributes: [.font: font])
    }

    private func elapsed() -> String {
        let seconds = Int(Date().timeIntervalSince(startedAt ?? Date()))
        let (hours, minutes) = (seconds / 3600, seconds / 60 % 60)
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, seconds % 60)
        }
        return String(format: "%02d:%02d", minutes, seconds % 60)
    }

    private func startTicking() {
        let ticker = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.show() }
        // a tenth of a second's slack, which is what lets the system fire this
        // alongside whatever else is due rather than waking for it alone
        ticker.tolerance = 0.1
        // the common modes, or the clock would freeze for as long as a menu is
        // held open — which is exactly when somebody is looking at it
        RunLoop.main.add(ticker, forMode: .common)
        self.ticker = ticker
    }

    private func stopTicking() {
        ticker?.invalidate()
        ticker = nil
    }

    /// The mark stepped through its frames while notes are being made.
    /// Transcribing a meeting takes minutes with nothing else on screen to show
    /// for it, and this is the only thing saying the work is still going rather
    /// than quietly dead.
    ///
    /// Reduce Motion is asked once, here, rather than on every frame, so a
    /// spell of processing animates one way from beginning to end. Somebody who
    /// changes the setting mid-transcription gets the new answer on the next
    /// memo, which is the one it can apply to.
    private func startShimmering() {
        stopShimmering()
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        shimmerFrames = reduceMotion ? Mark.workingSlowly : Mark.working
        let tick = reduceMotion ? Mark.breathTick : Mark.shimmerTick
        let shimmer = Timer(timeInterval: tick, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.shimmerFrame = (self.shimmerFrame + 1) % self.shimmerFrames.count
            self.show()
        }
        // the common modes, for the same reason the clock is added to them: a
        // menu held open must not be the thing that freezes the animation
        RunLoop.main.add(shimmer, forMode: .common)
        self.shimmer = shimmer
    }

    /// Back to the head of the wave as well as stopped, so the next spell of
    /// processing starts where every other one started instead of wherever the
    /// last one happened to be interrupted.
    private func stopShimmering() {
        shimmer?.invalidate()
        shimmer = nil
        shimmerFrame = 0
    }

    // --- the menu ------------------------------------------------------------

    /// Built fresh every time the menu is opened, so what it offers is what the
    /// recording can actually do right now. Nothing offers to start a second
    /// recording: the first one owns the microphone, and its notes are still
    /// being made until its process is gone. The pickers follow the same rule —
    /// they are only there when idle, since choosing a different microphone
    /// halfway through a meeting would change nothing about the tape running.
    /// The header over them says that in two words: what is set there is what
    /// the next recording will be made of, not this one.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard !previewing else {
            previewMenu(menu)
            return
        }
        switch state {
        case .idle:
            menu.addItem(action("Start Recording", #selector(startRecording), key: "r"))
            menu.addItem(action("Record for…", #selector(openDialFromMenu), key: "t"))
            // offered only once the dial has somewhere to come back from, so
            // that a dial nobody has moved is not followed by a line about it
            if rememberedPlace != .hanging || dialDock?.state.isHanging == false {
                menu.addItem(action("Return Dial to Menu Bar", #selector(returnDial)))
            }
            menu.addItem(.separator())
            menu.addItem(.sectionHeader(title: "Next Recording"))
            menu.addItem(picker("Project", projectItems()))
            menu.addItem(picker("Record from", deviceItems(
                devices?.outputs, "System mix (everything)",
                chosenOutput, #selector(chooseOutput)
            )))
            menu.addItem(picker("Microphone", deviceItems(
                devices?.inputs, "Default microphone",
                chosenInput, #selector(chooseInput)
            )))
            refresh()
        case .starting, .recording:
            // a rolling tape hardly ever gets here: the status item owns no menu
            // then and opens the panel instead. It can still happen in the
            // moment between "Starting …" being opened and the recorder saying
            // both streams are live, and a menu already on screen has to say
            // something true rather than the state it was opened in
            menu.addItem(action("Stop Recording", #selector(stopRecording), key: "r"))
            menu.addItem(note(state == .recording ? "\(heading()) — \(elapsed())" : "Starting …"))
        case .processing:
            menu.addItem(note("Processing memo …"))
        }
        menu.addItem(.separator())
        menu.addItem(action("Quit", #selector(quit), key: "q"))
    }

    /// One thing the menu offers to do. A key equivalent is shown only where it
    /// is true: this app has no window and no main menu, so ⌘R reaches nothing
    /// at all until the menu is open — which is also the only moment the
    /// shortcut is on screen making the promise.
    ///
    /// Starting and stopping share that key rather than splitting it between
    /// them. They are never offered together — the menu says one or the other,
    /// whichever the recording has left possible — so ⌘R means the tape, and
    /// what says which way it goes is the state the tape is in.
    private func action(_ title: String, _ selector: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        return item
    }

    /// A line that says what is happening rather than offering to do anything.
    private func note(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    // --- the panel a recording is watched in ----------------------------------

    /// A rolling tape is the one state with no menu. A menu is a list of things
    /// to choose, and mid-meeting there is exactly one — stop — with everything
    /// else on offer being the meters, which a menu can only ever show while it
    /// is held open. So the status item stops owning a menu and becomes a plain
    /// button that opens the panel.
    ///
    /// The tracking is cancelled first because the tape starts while somebody is
    /// very likely looking at "Starting …": a menu whose status item has let go
    /// of it stays on screen belonging to nothing.
    private func openToThePanel() {
        menu.cancelTracking()
        statusItem.menu = nil
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
    }

    /// The menu handed back. Setting it is what takes the click away from the
    /// button again — a status item with a menu opens it itself — and clearing
    /// the action first is what keeps a stale selector off a button that is no
    /// longer meant to answer one.
    private func openToTheMenu() {
        statusItem.button?.target = nil
        statusItem.button?.action = nil
        statusItem.menu = menu
    }

    /// The panel opens on a click and closes on the next one, the way the menu
    /// it replaced did. Nothing opens it by itself: a window appearing over a
    /// meeting that nobody asked for is the thing this has to not be.
    @objc private func togglePanel() {
        if panel != nil {
            closePanel()
        } else {
            openPanel()
        }
    }

    /// The panel put on screen with what the recording looks like right now,
    /// rather than left to fill in on the next reading — a tenth of a second of
    /// empty meters is a tenth of a second of looking broken.
    ///
    /// It must never take focus: this opens over a meeting, and an app that
    /// activates in front of one has taken the keyboard away from whoever was
    /// muting themselves with it. A non-activating borderless panel at the
    /// status bar's own level is the whole of how that is arranged, and joining
    /// every space is what puts it over a call that has gone full screen.
    private func openPanel() {
        guard state == .recording else { return }
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let view = RecordingPanelView(
            system: chosenOutput?.name ?? "system mix",
            microphone: chosenInput?.name ?? "default microphone",
            reduceMotion: reduceMotion
        )
        view.onStop(
            self, previewing ? #selector(stopPreviewing) : #selector(stopRecording)
        )
        // a pointer arriving inside the panel is somebody having taken it up on
        // what it is showing, and from that moment the window is theirs to
        // close: one that vanished out from under a pointer resting on it would
        // be this app deciding it had been looked at for long enough
        view.onEngage = { [weak self] in self?.stopGlancing() }
        view.show(system: systemLevels, microphone: microphoneLevels)
        view.showElapsed(elapsed())
        view.showFooter(system: systemLevels, microphone: microphoneLevels)
        let panel = floating(view, shadow: true)
        self.panel = panel
        self.panelView = view
        place(panel)
        arrive(panel, reduceMotion: reduceMotion) { view.appear() }
        // the button stays lit for as long as the panel is up, which is the only
        // thing tying the window under the menu bar to the item it came out of
        statusItem.button?.highlight(true)
        watchForClicks()
    }

    /// A window for a view to float in under the menu bar, built the same way
    /// for the island and for the dial: borderless, never activating, at the
    /// status bar's own level, on every space. The island asks the window for
    /// its shadow; the dial draws its own, because a shadow a window works out
    /// from transparent pixels is worked out again every time they change.
    private func floating(_ view: NSView, shadow: Bool, offscreen: Bool = false) -> NSPanel {
        let panel: NSPanel
        if offscreen {
            panel = DialPanel(
                contentRect: view.frame, styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered, defer: false
            )
            // this window slides on its own account; AppKit's guess at an
            // animation for it would only be something for that slide to fight
            panel.animationBehavior = .none
        } else {
            panel = NSPanel(
                contentRect: view.frame, styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered, defer: false
            )
        }
        panel.contentView = view
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = shadow
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        return panel
    }

    /// The panel put on screen as the status item growing downwards rather than
    /// as a window switched on: the whole thing fades up quickly, and the view
    /// inside it goes on growing into place long after it has, so what there is
    /// to watch is the island settling rather than a rectangle brightening. The
    /// window's frame never moves — it sits at the menu bar's own level, and a
    /// panel that slid down into place would be drawn over the menu bar for as
    /// long as it took to arrive.
    ///
    /// Reduce Motion gets the panel and none of this. The opening itself still
    /// happens either way: a window saying the tape is rolling is information,
    /// and it is only the way it arrives that is decoration.
    private func arrive(_ panel: NSPanel, reduceMotion: Bool, appear: () -> Void) {
        guard !reduceMotion else {
            panel.orderFrontRegardless()
            return
        }
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        appear()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = RecordingPanelView.fadingUp
            context.timingFunction = RecordingPanelView.easing
            panel.animator().alphaValue = 1
        }
    }

    /// Everything the panel was, undone in the order that leaves nothing
    /// behind: the clock on a panel that opened itself first, then the
    /// monitors, since a click arriving after the window is gone would try to
    /// close it again, then the light on the button, then the window. Safe to
    /// call on a panel that is not open, which is what lets every way out of a
    /// recording go through it.
    ///
    /// This app has let go of the window before it has finished going, so that
    /// a panel opened again during the fade is a new one built from scratch
    /// rather than a half-faded one caught and turned round.
    private func closePanel() {
        stopGlancing()
        let closing = panel
        panel = nil
        panelView = nil
        stopWatchingClicks()
        vanish(closing)
    }

    /// The monitors taken down once nothing is left for them to close, and the
    /// button's light with them: it is lit for whichever window is up, and goes
    /// out with the last of them.
    private func stopWatchingClicks() {
        guard panel == nil, dialDock?.state.isHanging != true else { return }
        if let clicksElsewhere {
            NSEvent.removeMonitor(clicksElsewhere)
        }
        if let clicksHere {
            NSEvent.removeMonitor(clicksHere)
        }
        clicksElsewhere = nil
        clicksHere = nil
        statusItem.button?.highlight(false)
    }

    /// Whichever floating window is up, closed. What a click somewhere else
    /// means is the same for both.
    private func closeFloating() {
        closePanel()
        // only a hanging dial goes on a click elsewhere: one that has been put
        // somewhere is a window somebody expects to find there
        if dialDock?.state.isHanging == true {
            closeDial()
        }
    }

    // --- the dial ---------------------------------------------------------------

    /// The dial opened from the menu, over an idle recorder only: it sets the
    /// length of the *next* recording, and there is no next one while a tape
    /// is rolling.
    @objc private func openDialFromMenu() {
        guard state == .idle else { return }
        // the way in that needs no pointer: a dial tucked into an edge comes
        // out, one somewhere on the screen comes to the front
        if let dialDock {
            dialDock.reveal()
            return
        }
        openDial()
    }

    /// The dial's place forgotten and the dial, if it is up, hung under the
    /// status item again — the way back for a window carried somewhere that
    /// turned out not to suit.
    @objc private func returnDial() {
        UserDefaults.standard.removeObject(forKey: Key.dialPlace)
        guard dial != nil else { return }
        closeDial()
        openDial()
    }

    /// The dial put on screen under the status item, set to whatever it was
    /// left at last time. Pressing its middle closes it and starts the tape for
    /// that long; turning it is remembered as it turns, so a dial dismissed by
    /// a click elsewhere still leaves the length it was turned to.
    private func openDial(at minutes: Int? = nil, remembering: Bool = true, place: DialPlace? = nil) {
        guard dial == nil else { return }
        closePanel()
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let view = DialView(minutes: minutes ?? chosenMinutes, reduceMotion: reduceMotion)
        // a preview's dial forgets: it is being turned to look at, and the
        // length somebody set for their next real meeting must survive that
        if remembering {
            view.onChange = { minutes in
                UserDefaults.standard.set(minutes, forKey: Key.minutes)
            }
        }
        view.onStart = { [weak self] minutes in
            guard let self else { return }
            self.closeDial()
            self.startTimedRecording(minutes)
        }
        let panel = floating(view, shadow: false, offscreen: true)
        self.dial = panel
        self.dialView = view
        let dock = DialDock(panel: panel, view: view, reduceMotion: reduceMotion)
        // carried off, the dial is no longer the menu-like thing a click
        // elsewhere dismisses, and the button it hung from lets go of it
        dock.onLeftHanging = { [weak self] in
            self?.stopWatchingClicks()
        }
        if remembering {
            dock.onPlaced = { place in
                UserDefaults.standard.set(place.encoded, forKey: Key.dialPlace)
            }
        }
        self.dialDock = dock
        // back where it was, or — for a dial that has never been moved, and
        // for one whose display is gone — under the status item like a menu
        if dock.restore(place ?? (remembering ? rememberedPlace : .hanging)) {
            return
        }
        self.place(panel)
        arrive(panel, reduceMotion: reduceMotion) { view.appear() }
        statusItem.button?.highlight(true)
        watchForClicks()
    }

    private func closeDial() {
        let closing = dial
        dial = nil
        dialView = nil
        dialDock = nil
        stopWatchingClicks()
        vanish(closing)
    }

    /// The tape stopped at the length it was asked for, by one timer set for
    /// that moment. A second's slack, because a recording that runs a second
    /// long is still the meeting and a wake-up of its own for the exact second
    /// is not worth having.
    private func scheduleStop() {
        guard let timedMinutes else {
            forgetDeadline()
            return
        }
        scheduleStop(after: TimeInterval(timedMinutes * 60))
    }

    private func scheduleStop(after seconds: TimeInterval) {
        stopAt?.invalidate()
        let deadline = Date().addingTimeInterval(seconds)
        self.deadline = deadline
        let stop = Timer(timeInterval: seconds, repeats: false) { [weak self] _ in
            self?.deadlineReached()
        }
        stop.tolerance = 1
        RunLoop.main.add(stop, forMode: .common)
        stopAt = stop
    }

    /// The length is up. A real tape is stopped the way the menu stops it; a
    /// preview moves on to the state a stopped tape lands in, since what is
    /// being watched is the app reacting to its own deadline, and a preview
    /// whose deadline came and went with nothing happening would be showing a
    /// timer that does not work.
    private func deadlineReached() {
        if previewing {
            showPreview(.processing)
        } else {
            stopRecording()
        }
    }

    private func forgetDeadline() {
        stopAt?.invalidate()
        stopAt = nil
        deadline = nil
        timedMinutes = nil
    }

    /// The window taken off screen, faded first wherever anything is allowed to
    /// move. It goes in the time it took to fade up and not the time it took to
    /// arrive: what the rest of arriving buys is the island growing, which is
    /// the part somebody watches, and a window that takes as long to leave is a
    /// window in the way of whatever the click that dismissed it was meant for.
    ///
    /// The completion handler is the only thing holding the panel by then, and
    /// that is deliberate: it keeps the window alive exactly as long as it
    /// takes to finish going and not an instant past it.
    private func vanish(_ panel: NSPanel?) {
        guard let panel else { return }
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
            panel.orderOut(nil)
            return
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = RecordingPanelView.vanishing
            context.timingFunction = RecordingPanelView.easing
            panel.animator().alphaValue = 0
        } completionHandler: {
            panel.orderOut(nil)
        }
    }

    /// The panel opened without being asked for, at the one moment there is
    /// something new to say: the tape has started rolling. It closes itself
    /// again a few seconds later, so what somebody gets is a glance at both
    /// meters moving rather than a window standing over their meeting for the
    /// length of it.
    ///
    /// Nothing is opened over a panel already up. There cannot be one — the
    /// status item owned a menu until a line ago and a menu opens no panel —
    /// and the guard is what keeps that true of any other way into this.
    private func showBriefly() {
        guard panel == nil else { return }
        openPanel()
        guard panel != nil else { return }
        let glance = Timer(timeInterval: AppDelegate.glance, repeats: false) { [weak self] _ in
            self?.closePanel()
        }
        // the common modes, for the same reason the clock and the shimmer are
        // put in them: a menu held open elsewhere must not be the thing that
        // leaves this window standing over a meeting
        RunLoop.main.add(glance, forMode: .common)
        glanceOver = glance
    }

    /// The clock on an uninvited panel stopped, however the panel got there
    /// first — closed, engaged with, or overtaken by the recording ending.
    /// Killing the timer rather than asking it when it fires whether the panel
    /// it was set for is still the one on screen is the whole of what makes a
    /// panel somebody opened for themselves safe from it.
    private func stopGlancing() {
        glanceOver?.invalidate()
        glanceOver = nil
    }

    /// A detached popover: centred on the button it belongs to and hanging a
    /// few points clear of the menu bar, the way a menu does. What ties it to
    /// the status item is where it is and the way it grows out of its own top
    /// edge, not a shared border — an opaque island pressed against the bar
    /// meets it at a bare square corner, which is a join that shows.
    ///
    /// Clamped to the screen, because a status item close to the right-hand end
    /// of the menu bar would otherwise centre a 300 point panel half off it.
    private func place(_ panel: NSPanel) {
        guard let anchor = statusItem.button?.window?.frame else { return }
        guard let screen = statusItem.button?.window?.screen ?? NSScreen.main else { return }
        let margin: CGFloat = 8
        let size = panel.frame.size
        let leftmost = screen.visibleFrame.minX + margin
        let rightmost = screen.visibleFrame.maxX - margin - size.width
        let x = min(max(anchor.midX - size.width / 2, leftmost), max(leftmost, rightmost))
        panel.setFrameOrigin(NSPoint(
            x: x.rounded(), y: (anchor.minY - AppDelegate.hanging - size.height).rounded()
        ))
    }

    /// What closes the panel: a click anywhere that is not the panel itself.
    /// The global monitor sees the clicks that go to other apps, the local one
    /// sees the clicks that come here — and the local one has to let the status
    /// item's own window through, or clicking the button a second time would
    /// close the panel a moment before the button's action reopened it.
    private func watchForClicks() {
        guard clicksElsewhere == nil, clicksHere == nil else { return }
        let elsewhere: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        clicksElsewhere = NSEvent.addGlobalMonitorForEvents(matching: elsewhere) { [weak self] _ in
            self?.closeFloating()
        }
        clicksHere = NSEvent.addLocalMonitorForEvents(matching: elsewhere) { [weak self] event in
            guard let self else { return event }
            let ours = [self.panel, self.dial, self.statusItem.button?.window]
            if !ours.contains(where: { $0 === event.window }) {
                self.closeFloating()
            }
            return event
        }
    }

    // --- the pickers ----------------------------------------------------------

    /// A submenu of things one of which is already chosen. It stays openable
    /// even while its only line is "loading …", so the person who opened it can
    /// see that the list is on its way rather than a dead menu item.
    private func picker(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        for entry in items {
            submenu.addItem(entry)
        }
        item.submenu = submenu
        return item
    }

    /// One thing that could be chosen: what it is called, whether it is what is
    /// chosen now, and the value the handler stores if it is clicked.
    private func choice(
        _ title: String, _ chosen: Bool, _ selector: Selector, _ value: Any?
    ) -> NSMenuItem {
        let item = action(title, selector)
        item.state = chosen ? .on : .off
        item.representedObject = value
        return item
    }

    /// Every project a memo has been filed under, with how many are in it. The
    /// chosen project is added when the list does not have it, which is the
    /// ordinary case on a fresh install: "other" is where memos go by default
    /// and no project exists until a memo is in it.
    private func projectItems() -> [NSMenuItem] {
        guard let projects else { return [note("loading …")] }
        let chosen = chosenProject
        var items = projects.map {
            choice("\($0.name) (\($0.count))", $0.name == chosen, #selector(chooseProject), $0.name)
        }
        if !projects.contains(where: { $0.name == chosen }) {
            items.append(choice(chosen, true, #selector(chooseProject), chosen))
        }
        return items
    }

    /// A picker for one direction of audio: the line that leaves the choice to
    /// the recorder first, then every device the Mac has that way. A remembered
    /// device that is not in the list is shown anyway, named as missing and
    /// still ticked — a recording started with it dies on the spot, so a stale
    /// choice is exactly the thing this menu has to let somebody see and change.
    private func deviceItems(
        _ listed: [AudioDevice]?, _ anything: String, _ chosen: AudioDevice?, _ choose: Selector
    ) -> [NSMenuItem] {
        guard let listed else { return [note("loading …")] }
        var items = [choice(anything, chosen == nil, choose, nil)]
        for device in listed {
            items.append(choice(device.name, device.uid == chosen?.uid, choose, device))
        }
        if let chosen, !listed.contains(where: { $0.uid == chosen.uid }) {
            items.append(choice("\(chosen.name) (not connected)", true, choose, chosen))
        }
        return items
    }

    // --- what the next recording is made of -----------------------------------

    /// The choices live in this app's own defaults rather than in vtn's config:
    /// picking a microphone here says what this button should do, not what every
    /// `vtn record` typed into a terminal should do. Each device is remembered
    /// by name as well as by UID, so the menu can still say which one it means
    /// while the thing is unplugged.
    private enum Key {
        static let project = "project"
        static let outputUID = "outputDeviceUID"
        static let outputName = "outputDeviceName"
        static let inputUID = "inputDeviceUID"
        static let inputName = "inputDeviceName"
        static let minutes = "recordMinutes"
        static let dialPlace = "dialPlace"
    }

    /// Where the dial was last left. Under the status item until somebody
    /// carries it somewhere, and there ever after — a window put in a corner is
    /// a window expected to be in that corner next time.
    private var rememberedPlace: DialPlace {
        DialPlace(encoded: UserDefaults.standard.string(forKey: Key.dialPlace) ?? "") ?? .hanging
    }

    /// Half an hour until the dial has been turned: the length a meeting is
    /// booked for when nobody thought about it.
    private var chosenMinutes: Int {
        let stored = UserDefaults.standard.integer(forKey: Key.minutes)
        return stored == 0 ? 30 : stored
    }

    private var chosenProject: String {
        UserDefaults.standard.string(forKey: Key.project) ?? "other"
    }

    private var chosenOutput: AudioDevice? { remembered(Key.outputUID, Key.outputName) }

    private var chosenInput: AudioDevice? { remembered(Key.inputUID, Key.inputName) }

    /// Nothing remembered means the recorder is left to decide — the whole
    /// system mix, and whichever microphone macOS calls the default one.
    private func remembered(_ uidKey: String, _ nameKey: String) -> AudioDevice? {
        guard let uid = UserDefaults.standard.string(forKey: uidKey), !uid.isEmpty else {
            return nil
        }
        return AudioDevice(uid: uid, name: UserDefaults.standard.string(forKey: nameKey) ?? uid)
    }

    private func remember(_ device: AudioDevice?, _ uidKey: String, _ nameKey: String) {
        guard let device else {
            UserDefaults.standard.removeObject(forKey: uidKey)
            UserDefaults.standard.removeObject(forKey: nameKey)
            return
        }
        UserDefaults.standard.set(device.uid, forKey: uidKey)
        UserDefaults.standard.set(device.name, forKey: nameKey)
    }

    @objc private func chooseProject(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        UserDefaults.standard.set(name, forKey: Key.project)
    }

    @objc private func chooseOutput(_ sender: NSMenuItem) {
        remember(sender.representedObject as? AudioDevice, Key.outputUID, Key.outputName)
    }

    @objc private func chooseInput(_ sender: NSMenuItem) {
        remember(sender.representedObject as? AudioDevice, Key.inputUID, Key.inputName)
    }

    // --- keeping the pickers' lists to hand ------------------------------------

    /// Both lists come out of a subprocess, and `vtn projects` opens the memo
    /// store through Python — far too slow to run while a menu is being drawn.
    /// So the menu never asks for anything: it shows what was fetched last, and
    /// each fetch is for the next time it opens. Fetching happens at launch, on
    /// every opening, and when a recording ends, since that is the moment the
    /// store may hold a project that did not exist before.
    private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        DispatchQueue.global(qos: .utility).async {
            let projects = self.readProjects()
            let devices = self.readDevices()
            DispatchQueue.main.async {
                self.projects = projects
                self.devices = devices
                self.refreshing = false
            }
        }
    }

    /// The projects, as `vtn projects` prints them: a name and a count, tab
    /// apart. Any line that is not that is dropped rather than guessed at.
    private func readProjects() -> [(name: String, count: Int)] {
        guard let vtn = vtnCommand(), let printed = output(of: vtn, ["projects"]) else {
            return []
        }
        return printed.split(separator: "\n").compactMap { line in
            let fields = line.components(separatedBy: "\t")
            guard fields.count == 2, let count = Int(fields[1]), !fields[0].isEmpty else {
                return nil
            }
            return (name: fields[0], count: count)
        }
    }

    /// The devices, as the capture helper prints them: direction, UID and name,
    /// tabs apart. It is the same binary the recording itself runs, and asking
    /// it what exists opens no tap and prompts nobody for permission — which is
    /// what lets these pickers be filled in before anything has been recorded.
    private func readDevices() -> AudioDevices {
        let helper = vtnHome().appendingPathComponent("bin/vtn-capture").path
        guard let printed = output(of: helper, ["--list-devices"]) else {
            return AudioDevices(inputs: [], outputs: [])
        }
        var inputs: [AudioDevice] = []
        var outputs: [AudioDevice] = []
        for line in printed.split(separator: "\n") {
            let fields = line.components(separatedBy: "\t")
            guard fields.count == 3, !fields[1].isEmpty else { continue }
            let device = AudioDevice(uid: fields[1], name: fields[2])
            if fields[0] == "in" {
                inputs.append(device)
            } else if fields[0] == "out" {
                outputs.append(device)
            }
        }
        return AudioDevices(inputs: inputs, outputs: outputs)
    }

    /// Everything a command printed, or nothing if it could not be run or came
    /// back unhappy — a half-read list is not worth showing as a menu. Reading
    /// the pipe out before waiting on the exit is what keeps a long list from
    /// filling the buffer and leaving both sides waiting on each other. Runs off
    /// the main thread only.
    private func output(of executable: String, _ arguments: [String]) -> String? {
        guard FileManager.default.isExecutableFile(atPath: executable) else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = recorderEnvironment()
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let printed = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        return String(decoding: printed, as: UTF8.self)
    }

    // --- driving the recorder ------------------------------------------------

    /// A recording that runs until it is stopped.
    @objc private func startRecording() {
        timedMinutes = nil
        launchRecorder()
    }

    /// A recording that stops itself after this many minutes of tape.
    private func startTimedRecording(_ minutes: Int) {
        timedMinutes = minutes
        launchRecorder()
    }

    private func launchRecorder() {
        // nothing in a preview's menu offers this, and this guard is what keeps
        // that from being the only thing standing between a preview and a real
        // tape of whatever is being said in the room
        guard !previewing else { return }
        guard state == .idle else { return }
        guard let vtn = vtnCommand() else {
            warn(
                "vtn is not installed",
                "Install voice-to-note and run `vtn setup`, then open this app again."
            )
            return
        }
        let recorder = Process()
        recorder.executableURL = URL(fileURLWithPath: vtn)
        recorder.arguments = recordArguments()
        recorder.environment = recorderEnvironment()
        let errors = Pipe()
        recorder.standardError = errors
        errors.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            DispatchQueue.main.async { self.heard(data) }
        }
        recorder.terminationHandler = { finished in
            let code = finished.terminationStatus
            DispatchQueue.main.async { self.ended(code) }
        }
        do {
            try recorder.run()
        } catch {
            warn("vtn could not be started", error.localizedDescription)
            return
        }
        self.recorder = recorder
        self.errors = errors
        state = .starting
        show()
    }

    /// The same command line a person would type, made out of what the pickers
    /// were left set to. A device is named only when one was chosen: leaving the
    /// flag off is what asks the recorder for the system mix and the default
    /// microphone, and passing a UID for either is what overrides that.
    ///
    /// `--levels` is the one flag nobody would type: the recorder draws its own
    /// bars for a terminal, sees a pipe here and would draw nothing at all, and
    /// the numbers it prints instead are what the panel's waveforms are made of.
    private func recordArguments() -> [String] {
        var arguments = ["record", "--project", chosenProject, "--levels"]
        if let output = chosenOutput {
            arguments += ["--output-device", output.uid]
        }
        if let input = chosenInput {
            arguments += ["--input-device", input.uid]
        }
        return arguments
    }

    @objc private func stopRecording() {
        // a preview's panel is wired to `stopPreviewing` instead; this is the
        // second lock on the same door
        guard !previewing else { return }
        guard state == .starting || state == .recording else { return }
        guard let recorder, recorder.isRunning else { return }
        // the same interrupt a Ctrl+C in a terminal sends: the recorder closes
        // the two tracks and then spends minutes merging and transcribing them,
        // which is why stopping the tape is not the end of the session
        recorder.interrupt()
        stopTicking()
        forgetDeadline()
        state = .processing
        show()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    /// Everything the recorder says on its way through a meeting: the meter's
    /// readings, which feed the panel and go no further, and every other line,
    /// which is kept whole in the log and read for the one that matters here —
    /// printed only once both audio streams are live, so it is what the red dot
    /// waits for.
    private func heard(_ data: Data) {
        unread += String(decoding: data, as: UTF8.self)
        while let newline = unread.firstIndex(of: "\n") {
            let line = String(unread[..<newline])
            unread = String(unread[unread.index(after: newline)...])
            if let reading = levels(in: line) {
                // ten of these a second, and none of them logged: an hour of
                // meeting would be thirty-six thousand lines of numbers nobody
                // will read, and the one line saying what went wrong would be
                // somewhere in the middle of them. A reading arriving before
                // the tape rolls is dropped as well — it describes nothing
                // that is being recorded yet
                if state == .recording {
                    metered(reading.system, reading.microphone)
                }
                continue
            }
            write(line)
            if state == .starting, line.hasPrefix("recording —") {
                startedAt = Date()
                state = .recording
                startTicking()
                scheduleStop()
                show()
            }
        }
    }

    /// A reading from the recorder's meter, or nothing if this line is anything
    /// else: `level`, the system side and the microphone side, tabs apart and
    /// in dBFS. A line short of a field or carrying something that is not a
    /// number is dropped rather than guessed at, the same as the device list —
    /// a meter is not worth a wrong number.
    private func levels(in line: String) -> (system: Double, microphone: Double)? {
        guard line.hasPrefix("level\t") else { return nil }
        let fields = line.components(separatedBy: "\t")
        guard fields.count == 3, let system = Double(fields[1]),
            let microphone = Double(fields[2])
        else {
            return nil
        }
        return (system: system, microphone: microphone)
    }

    /// The recorder is gone, however it went: stopped from this menu and then
    /// finished with the notes, or fallen over partway through a meeting. Either
    /// way this app is free to start another one, and the person who is not
    /// looking at the menu bar is told which of the two it was.
    private func ended(_ code: Int32) {
        if !unread.isEmpty {
            write(unread)
            unread = ""
        }
        errors?.fileHandleForReading.readabilityHandler = nil
        errors = nil
        recorder = nil
        startedAt = nil
        stopTicking()
        forgetDeadline()
        state = .idle
        show()
        refresh()
        if code == 0 {
            notify("Memo processed", "The meeting is transcribed and its notes are written.")
        } else {
            notify("Recording failed", "vtn exited with code \(code) — see \(logURL.path)")
        }
    }

    /// The vtn to run. The places an install puts it come first, in that order;
    /// anything else is found the way `/usr/bin/env vtn` would find it, by
    /// walking the same PATH the recorder itself is given — which is what lets
    /// this app say "not installed" up front instead of leaving a child that
    /// exited 127 in a log file nobody is reading.
    private func vtnCommand() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let installed = ["\(home)/.local/bin/vtn", "/opt/homebrew/bin/vtn", "/usr/local/bin/vtn"]
        let searched = searchPath().split(separator: ":").map { "\($0)/vtn" }
        for path in installed + searched where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        return nil
    }

    /// An app opened from the Finder inherits a bare PATH with none of the
    /// places a person installs tools, and the pipeline shells out to ffmpeg and
    /// to the claude CLI. Without this the meeting is taped and then fails the
    /// moment anything is made of it.
    private func searchPath() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let inherited = ProcessInfo.processInfo.environment["PATH"] ?? ""
        return inherited + ":/opt/homebrew/bin:/usr/local/bin:" + home + "/.local/bin"
    }

    private func recorderEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = searchPath()
        return environment
    }

    // --- driving a preview ----------------------------------------------------

    /// One state this app can be in, as a thing that can be picked off a menu.
    /// A preview exists for the states nobody can reach on purpose — a
    /// microphone that has died mid-meeting, ten seconds of quiet on both
    /// sides, the minutes of processing after the tape stops — since the only
    /// other way to look at one of those is to tape a real meeting and then
    /// arrange for it to go wrong.
    private enum Scenario: CaseIterable {
        case idle
        case dial
        case dialFull
        case dialTucked
        case starting
        case voices
        case timed
        case timedEnding
        case microphoneDead
        case bothSilent
        case processing

        var title: String {
            switch self {
            case .idle: return "Idle"
            case .dial: return "Idle — dial open"
            case .dialFull: return "Idle — dial at two hours"
            case .dialTucked: return "Idle — dial tucked into the right edge"
            case .starting: return "Starting"
            case .voices: return "Recording — voices"
            case .timed: return "Recording — timed, 20 min left"
            case .timedEnding: return "Recording — timed, stops in 45 s"
            case .microphoneDead: return "Recording — microphone dead"
            case .bothSilent: return "Recording — both silent"
            case .processing: return "Processing"
            }
        }

        /// The state the app is genuinely put into. A scenario names a state
        /// and nothing else: the mark, the shimmer, the panel showing itself,
        /// the meters being emptied all follow from it as the app's own doing,
        /// which is the whole of what makes a preview worth looking at rather
        /// than a picture of one.
        var state: RecorderState {
            switch self {
            case .idle, .dial, .dialFull, .dialTucked: return .idle
            case .starting: return .starting
            case .voices, .timed, .timedEnding, .microphoneDead, .bothSilent: return .recording
            case .processing: return .processing
            }
        }

        /// What each side of the tape is doing, or nothing where no tape is
        /// rolling. The two sides never share a seed: two meters drawing the
        /// same shape at the same moment is the one thing a meeting never looks
        /// like, and it would be the first thing to disbelieve.
        var sides: (system: Rehearsal.Side, microphone: Rehearsal.Side)? {
            switch self {
            case .voices, .timed, .timedEnding: return (.talking(seed: 7), .talking(seed: 31))
            case .microphoneDead: return (.talking(seed: 7), .quiet(seed: 5))
            case .bothSilent: return (.quiet(seed: 11), .quiet(seed: 5))
            case .idle, .dial, .dialFull, .dialTucked, .starting, .processing: return nil
            }
        }

        /// How long a timed scenario's tape has left, or nothing for one that
        /// runs until stopped. Forty-five seconds is short enough to sit and
        /// watch the deadline arrive, and long enough to read the heading it
        /// wears on the way there.
        var secondsLeft: TimeInterval? {
            switch self {
            case .timed: return 20 * 60
            case .timedEnding: return 45
            default: return nil
            }
        }

        /// The dial's setting where the scenario is a dial, or nothing.
        var dialMinutes: Int? {
            switch self {
            case .dialFull: return DialView.longest
            default: return nil
            }
        }

        var isDial: Bool {
            switch self {
            case .dial, .dialFull, .dialTucked: return true
            default: return false
            }
        }

        /// Where the scenario's dial is put, on the main screen: tucked into
        /// an edge to be hovered out of it, or hanging like any other.
        var dialPlace: DialPlace {
            guard self == .dialTucked, let main = NSScreen.main,
                  let display = DialDock.number(of: main) else { return .hanging }
            return .tucked(.right, display: display, along: 0.5)
        }
    }

    /// The menu a preview is driven from: every state, one to a line, with the
    /// one on screen ticked. It replaces the ordinary menu rather than being
    /// added to it — there is no recorder here to start, and no sense in
    /// setting pickers for a recording that will never happen.
    ///
    /// The panel's two lines are offered only where there is a panel to open,
    /// and greyed out rather than taken away in the other states, so the menu
    /// is the same length in all six and picking down it does not move under
    /// the pointer.
    private func previewMenu(_ menu: NSMenu) {
        menu.addItem(.sectionHeader(title: "Preview"))
        for scenario in Scenario.allCases {
            menu.addItem(choice(
                scenario.title, scenario == previewScenario, #selector(choosePreview), scenario
            ))
        }
        menu.addItem(.separator())
        let rolling = previewScenario.state == .recording
        for item in [
            action("Show Panel", #selector(togglePanel)),
            action("Replay Arrival", #selector(replayArrival)),
        ] {
            item.isEnabled = rolling
            menu.addItem(item)
        }
        menu.addItem(.separator())
        menu.addItem(action("Quit Preview", #selector(quit), key: "q"))
    }

    /// A scenario put on screen by the route the real thing takes: the state is
    /// set, and its own didSet does everything after that. A recording is
    /// reached the way a recording is reached, so the panel shows itself
    /// unasked, the clock counts from this moment, and what somebody is looking
    /// at is the app rather than an arrangement of it.
    private func showPreview(_ scenario: Scenario) {
        previewScenario = scenario
        // whatever the last scenario left running is stopped first, and by the
        // same two calls the end of a real recording makes: a second clock
        // ticking for a tape that has been swapped out would count from the
        // wrong moment and never stop
        stopRehearsing()
        stopTicking()
        forgetDeadline()
        startedAt = nil
        guard let sides = scenario.sides else {
            state = scenario.state
            show()
            // the dial by the door it is opened from, and its middle wired to
            // nothing but closing it: a preview starts no tape
            if scenario.isDial {
                openDial(at: scenario.dialMinutes, remembering: false, place: scenario.dialPlace)
                dialView?.onStart = { [weak self] _ in self?.closeDial() }
            }
            return
        }
        // through `.starting` on the way rather than straight to `.recording`,
        // because that is the state a real recording passes through, and its
        // didSet is what empties the meters and takes down the panel the
        // scenario before this one left standing
        state = .starting
        startedAt = Date()
        state = .recording
        startTicking()
        // a timed scenario is given its deadline the way a real tape is given
        // one — after the tape is rolling — so the heading counts down and the
        // deadline, when it comes, moves the preview on by itself
        if let left = scenario.secondsLeft {
            scheduleStop(after: left)
        }
        show()
        startRehearsing(sides)
    }

    @objc private func choosePreview(_ sender: NSMenuItem) {
        guard let scenario = sender.representedObject as? Scenario else { return }
        showPreview(scenario)
    }

    /// The panel closed and opened again by the path that opens it unasked, so
    /// that the one thing in this app which happens once and cannot be asked
    /// for — the island arriving out of the status item — can be watched more
    /// than once. Closing first is what makes it an arrival: `showBriefly`
    /// opens nothing over a panel already up, and rightly so.
    @objc private func replayArrival() {
        closePanel()
        showBriefly()
    }

    /// What the panel's Stop button does in a preview: pick the Idle scenario,
    /// the same way the menu picks it. The real button goes to Processing
    /// instead, but what this one is being watched for is the panel leaving and
    /// the mark going out — and a button that moved a preview to a state its own
    /// menu was not ticking would be two things disagreeing about which scenario
    /// is on screen. Processing is a line in that menu for whoever wants it.
    @objc private func stopPreviewing() {
        showPreview(.idle)
    }

    /// The readings a preview is fed, at the rate the recorder prints them. Ten
    /// a second is not decoration: the waveform's span is counted in readings
    /// rather than in seconds and the silence clock counts real ones, so any
    /// other rate would draw a meeting that is not the length it says it is.
    ///
    /// The step lives in the closure rather than on this object, so it is born
    /// and dies with the timer and no scenario can start from wherever the last
    /// one was stopped.
    private func startRehearsing(
        _ sides: (system: Rehearsal.Side, microphone: Rehearsal.Side)
    ) {
        stopRehearsing()
        let system = Rehearsal(sides.system)
        let microphone = Rehearsal(sides.microphone)
        var step = 0
        let rehearsal = Timer(timeInterval: Rehearsal.interval, repeats: true) { [weak self] _ in
            self?.metered(system.level(at: step), microphone.level(at: step))
            step += 1
        }
        // the common modes, for the same reason the clock is put in them: this
        // menu is held open for as long as it takes to read six scenarios, and
        // meters that froze underneath it would be the very thing being judged
        RunLoop.main.add(rehearsal, forMode: .common)
        self.rehearsal = rehearsal
    }

    private func stopRehearsing() {
        rehearsal?.invalidate()
        rehearsal = nil
    }

    // --- telling the person what happened ------------------------------------

    private func askToNotify() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) {
            granted, _ in
            DispatchQueue.main.async { self.mayNotify = granted }
        }
    }

    /// How a session that ended while nobody was looking gets reported. Refused
    /// permission is left alone rather than nagged about: the menu bar icon
    /// still says everything this would have.
    private func notify(_ title: String, _ body: String) {
        guard mayNotify else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(
            identifier: UUID().uuidString, content: content, trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    /// For the two failures that happen before a recording exists, where there
    /// is nothing in the menu bar to read and nothing in the log yet either.
    private func warn(_ message: String, _ detail: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.runModal()
    }

    /// Keeps every line the recorder printed. A menu bar has nowhere to show the
    /// pipeline's own progress and failures, so this file is where a failure
    /// notification sends the person who wants to know what went wrong.
    private func write(_ line: String) {
        guard let data = (line + "\n").data(using: .utf8) else { return }
        if logFile == nil {
            let directory = logURL.deletingLastPathComponent()
            try? FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: true
            )
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            logFile = try? FileHandle(forWritingTo: logURL)
            _ = try? logFile?.seekToEnd()
        }
        try? logFile?.write(contentsOf: data)
    }
}

// MARK: - Meters and marks (drawing and models only; no app state, no process handling)

// imported again here, redundantly for this file, because everything between
// this line and the end of the section is meant to compile on its own: lifted
// out into a file of its own it renders the whole look of the meters offscreen,
// which is the only way to see what a recording will look like without taping
// one first.
import AppKit

/// The mark this app is known by in the menu bar, drawn here rather than
/// borrowed from SF Symbols: `waveform` is symmetric and already stands for
/// a dozen other audio apps, and the one job this glyph has is being picked
/// out of a crowded menu bar without being read. So it is five bars of
/// deliberately uneven height, and the shape never changes between states —
/// only its colour, and a wave of brightness travelling across it while notes
/// are being made. What is in the corner of the screen stays the same thing all
/// the way through a meeting; what it is doing is told by how it is inked.
///
/// Each image is built once and kept. A status item redraws its button on
/// every appearance change, once a second while a recording is running and
/// fifteen times a second while notes are being made, and none of that should
/// cost a fresh bitmap.
enum Mark {
    /// A status item scales its image to the height of the menu bar, and
    /// 18pt square is the size that arrives there unscaled — anything else
    /// is resampled, which is exactly what a shape made of nothing but
    /// straight edges cannot survive.
    private static let side: CGFloat = 18
    private static let barWidth: CGFloat = 2
    private static let gap: CGFloat = 1.5

    /// ▁▃█▅▂ — low, mid, tall, high, low. Even numbers only: the bars are
    /// centred vertically, so an odd height would put both ends of every
    /// bar on a half point, and a half-point edge is a grey smear rather
    /// than a line on a display that is not retina.
    private static let resting: [CGFloat] = [4, 8, 14, 10, 6]

    static let idle = draw(resting, .black, template: true, "Not recording")

    /// Half-inked, because the tape is not rolling yet. A template image is
    /// tinted through its own alpha, so drawing the mark faint is all it
    /// takes to have the menu bar show it faint in either appearance.
    static let starting = draw(
        resting, NSColor.black.withAlphaComponent(0.5), template: true, "Starting to record"
    )

    /// The one thing on screen saying a meeting is being taped, so it keeps
    /// its own colour instead of being tinted to match the menu bar the way
    /// everything else here is. Red is not decoration: it is the colour
    /// macOS itself uses for a live capture, and borrowing it means this
    /// needs no learning.
    static let recording = draw(resting, .systemRed, template: false, "Recording")

    // --- the mark while notes are being made ---------------------------------

    /// How faint a bar goes and how solid it comes back. Neither end is
    /// off: the mark has to stay the same five bars through the whole
    /// cycle, and one that faded out would make it a four-bar mark for
    /// part of every pass. Neither end is full either — a run to solid and
    /// back is a strobe in the corner of a screen somebody is working in.
    private static let dimmest: CGFloat = 0.4
    private static let brightest: CGFloat = 0.9

    /// How many frames one pass of the wave is cut into and how long the
    /// pass takes. Twenty-four over 1.6 seconds is a frame every 67 ms —
    /// fifteen a second, which is where a fading edge stops stepping and
    /// starts sliding. The pass itself is slow on purpose: movement caught
    /// at the edge of an eye says work is under way, and anything quicker
    /// is a thing demanding to be looked at.
    private static let frames = 24
    private static let pass: TimeInterval = 1.6

    /// The same brightening under Reduce Motion, over a longer breath and
    /// at a fraction of the frames — nothing travels across it, so there
    /// is no edge to keep smooth and no reason to redraw fifteen times a
    /// second for a mark that changes as one thing.
    private static let slowFrames = 4
    private static let breath: TimeInterval = 2

    /// How often the button wants the next frame. The timer belongs with
    /// the app's state and these numbers belong here, so what crosses
    /// between the two is an interval and nothing else.
    static let shimmerTick = pass / Double(frames)
    static let breathTick = breath / Double(slowFrames)

    /// What the mark says while notes are being made, on every frame of
    /// it: the image on the button is replaced fifteen times a second and
    /// a screen reader describes whichever one it happens to find there.
    private static let makingNotes = "Making notes from the meeting"

    /// A wave of brightness travelling across the five bars, one frame at
    /// a time. Only the ink moves. A height has to land on a whole point
    /// or its ends smear, which caps a shape animation at a handful of
    /// visibly different drawings and makes stepping between them a
    /// glitchy toggle; an alpha has no such rule and can be anything
    /// between the two ends of a sine, so this is smooth where a mark
    /// changing shape cannot be.
    static let working: [NSImage] = (0..<frames).map { travelling($0) }

    /// Reduce Motion gets the brightening and none of the travel: every
    /// bar on the same point of the wave, so the mark swells and settles
    /// as one thing and nothing crosses it. A mark held still was the
    /// other way, and it answers the one question this state exists to
    /// answer — whether the work is still going — with nothing at all,
    /// where a slow change of ink is the cross-fade that setting asks
    /// movement to be replaced by.
    static let workingSlowly: [NSImage] = (0..<slowFrames).map { breathing($0) }

    /// One frame of the travelling wave: every bar at its own point on the
    /// same sine, each lagging the one to its left by a fifth of a turn, so
    /// a crest runs from the left of the mark to the right and comes round
    /// again.
    private static func travelling(_ frame: Int) -> NSImage {
        let reached = 2 * Double.pi * Double(frame) / Double(frames)
        let inks = resting.indices.map { bar in
            faded(sin(reached - 2 * Double.pi * Double(bar) / Double(resting.count)))
        }
        return draw(resting, inks, template: true, makingNotes)
    }

    /// One frame of the Reduce Motion breath: the whole mark on one point
    /// of the sine, so it brightens and dims as a single thing.
    private static func breathing(_ frame: Int) -> NSImage {
        let together = faded(sin(2 * Double.pi * Double(frame) / Double(slowFrames)))
        return draw(
            resting, Array(repeating: together, count: resting.count),
            template: true, makingNotes
        )
    }

    /// A point on the sine turned into ink. A template image is tinted
    /// through its own alpha, so drawing a bar faint is the whole of how
    /// the menu bar is asked to show it faint, in either appearance.
    private static func faded(_ wave: Double) -> NSColor {
        let swing = (brightest - dimmest) / 2
        return NSColor.black.withAlphaComponent(dimmest + swing + swing * CGFloat(wave))
    }

    /// One frame of the mark: bars of the given heights, each in its own
    /// ink, rounded at both ends, laid out from a left edge that centres
    /// the whole run inside the square, so every frame sits in exactly the
    /// same place and what animates is how the bars are inked rather than
    /// the mark shifting sideways.
    private static func draw(
        _ heights: [CGFloat], _ inks: [NSColor], template: Bool, _ description: String
    ) -> NSImage {
        let run = CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * gap
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            var x = (side - run) / 2
            for (index, height) in heights.enumerated() {
                inks[index].set()
                let bar = NSRect(x: x, y: (side - height) / 2, width: barWidth, height: height)
                let cap = barWidth / 2
                NSBezierPath(roundedRect: bar, xRadius: cap, yRadius: cap).fill()
                x += barWidth + gap
            }
            return true
        }
        image.isTemplate = template
        image.accessibilityDescription = description
        return image
    }

    /// The whole mark in one ink, which is every state but the shimmer.
    private static func draw(
        _ heights: [CGFloat], _ ink: NSColor, template: Bool, _ description: String
    ) -> NSImage {
        draw(
            heights, Array(repeating: ink, count: heights.count),
            template: template, description
        )
    }
}

/// How loud one side of a recording has been over the last couple of seconds.
/// Two of these — the system tap and the microphone — are the whole model both
/// meters are drawn from: the panel's waveform draws the ring, the track it
/// falls back to under Reduce Motion draws `latest`, and the silence clock is
/// what lets either of them say a microphone has been dead for twelve seconds
/// rather than only that it happens to be quiet at this instant.
///
/// The time is passed in rather than read in here, so a reading times its own
/// silence — and so a scripted meeting can be walked through without waiting
/// out the seconds it describes.
final class LevelHistory {
    /// dBFS, the way the recorder reports it: 0 is as loud as a sample can be
    /// and −60 is what it prints for nothing at all. Under −50 is taken for
    /// silence rather than a quiet room, because a room with anybody in it
    /// sits well above that and a muted microphone sits on the floor.
    static let silence: Double = -50
    static let floor: Double = -60
    static let ceiling: Double = 0

    /// The newest reading, which is what a level indicator wants; the ring
    /// behind it is what a scrolling waveform wants.
    private(set) var latest = LevelHistory.floor

    /// When this side went quiet, or nothing while it is not. Kept as the
    /// moment rather than a running count so nothing has to tick to age it:
    /// whoever asks does the subtraction.
    private(set) var silentSince: Date?

    private var ring: [Double]

    /// The slot the next reading overwrites, which is also the oldest one.
    private var next = 0

    /// Filled with the floor rather than left short, so the waveform has a
    /// whole tray to draw from its first frame. Before any reading has arrived
    /// there genuinely is no sound, and drawing that as silence is honest; a
    /// row that grew in from the right would instead be claiming the meeting is
    /// younger than it is every time this is emptied.
    ///
    /// The ring is exactly as wide as the waveform, because the waveform is the
    /// only thing drawn from it.
    init(columns: Int = Waveform.columns) {
        ring = Array(repeating: LevelHistory.floor, count: max(columns, 1))
    }

    func push(_ level: Double, now: Date = Date()) {
        ring[next] = level
        next = (next + 1) % ring.count
        latest = level
        if level < LevelHistory.silence {
            // only the first silent reading starts the clock: the ones behind
            // it are the same silence going on, and restarting it ten times a
            // second would hold the count at zero for as long as it lasted
            if silentSince == nil {
                silentSince = now
            }
        } else {
            silentSince = nil
        }
    }

    /// Back to silence for the next recording, rather than a new history for
    /// it: the two are held for the life of the app, and what must not survive
    /// a recording is the readings, not the object.
    func reset() {
        for slot in ring.indices {
            ring[slot] = LevelHistory.floor
        }
        next = 0
        latest = LevelHistory.floor
        silentSince = nil
    }

    /// The readings oldest first. Copied out rather than handed over, because
    /// whatever draws them runs later — an image's drawing handler runs when
    /// the button is drawn, by which time more readings have landed in the
    /// ring — and a waveform has to be one moment rather than a smear of two.
    var trace: [Double] {
        var ordered = [Double]()
        ordered.reserveCapacity(ring.count)
        for step in 0..<ring.count {
            ordered.append(ring[(next + step) % ring.count])
        }
        return ordered
    }

    /// How long this side has been silent, or nothing if it is not.
    func silentFor(now: Date = Date()) -> TimeInterval? {
        guard let silentSince else { return nil }
        return now.timeIntervalSince(silentSince)
    }
}

/// A meeting that never happened, in numbers: the readings a preview is fed in
/// place of a tape. It lives here among the meters because it is the same kind
/// of thing they are — a model of how loud a side has been — and because what
/// it is for is judging the drawing, which is what everything in this section
/// is for.
///
/// Stepped rather than timed, and seeded rather than random. The same step of
/// the same side is the same number in every run, so a preview that looked
/// wrong can be opened again and looked at, and a change to the drawing is the
/// only thing that can have moved between two viewings.
struct Rehearsal {
    /// What one side of the tape is doing. Talking is phrases of syllables with
    /// gaps between them that dip under the silence floor, which is what a room
    /// with somebody in it does; quiet is a dead line well under that floor,
    /// which is a muted microphone — and telling those two apart is the whole
    /// job of the footer's silence clock.
    enum Side {
        case talking(seed: UInt64)
        case quiet(seed: UInt64)
    }

    /// The rate the recorder prints readings at, and so the rate a preview has
    /// to play them back at.
    static let interval: TimeInterval = 0.1

    /// How much of a meeting is written before the stream starts round again.
    /// Two minutes is longer than anybody watches one meter, so the loop is
    /// never the thing being judged.
    private static let leastSteps = 1200

    /// The shape of one syllable: a fast attack and a slower fall, as a
    /// fraction of the way from the silence floor up to the phrase's own peak.
    /// Five readings is half a second, which is about what a spoken syllable
    /// takes and what makes a waveform read as speech rather than as a fence.
    private static let syllable: [Double] = [0.34, 0.78, 1, 0.84, 0.46]

    private let levels: [Double]

    init(_ side: Side) {
        switch side {
        case let .talking(seed):
            var rng = SplitMix64(seed)
            levels = Rehearsal.talking(&rng)
        case let .quiet(seed):
            var rng = SplitMix64(seed)
            levels = Rehearsal.quiet(&rng)
        }
    }

    /// The reading for one step. The stream runs round rather than ending: a
    /// preview is watched for as long as somebody keeps looking at it, and a
    /// generator that ran out would leave both meters flat halfway through
    /// being judged — which is a state this app has a scenario of its own for.
    ///
    /// Any step at all is a step, below zero included. This is an index into a
    /// loop and a loop has no first reading, so there is nothing here for a
    /// caller to get wrong — which is worth the one extra line in the one type
    /// whose reason to exist is being driven from code written to try something.
    func level(at step: Int) -> Double {
        let slot = step % levels.count
        return levels[slot < 0 ? slot + levels.count : slot]
    }

    /// A voice: phrases of a few syllables each, separated by gaps that fall
    /// under the silence floor. The gaps are short — under a second — because
    /// silence is only worth reporting after ten of them, and a preview of two
    /// people talking must not keep tripping the clock that is there to say a
    /// microphone has died.
    ///
    /// It is left however long the last phrase leaves it rather than cut to a
    /// length, so the point it runs round at is always inside a gap and the
    /// loop never joins one syllable onto the middle of another.
    private static func talking(_ rng: inout SplitMix64) -> [Double] {
        var levels = [Double]()
        while levels.count < leastSteps {
            for _ in 0..<(2 + Int(rng.next(4))) {
                // −30 to −12 dBFS: a voice at conversational loudness, which is
                // well clear of the floor and well short of clipping
                let peak = -30 + Double(rng.next(1800)) / 100
                for shape in syllable {
                    levels.append(LevelHistory.silence + (peak - LevelHistory.silence) * shape)
                }
            }
            for _ in 0..<(3 + Int(rng.next(5))) {
                levels.append(-52 - Double(rng.next(600)) / 100)
            }
        }
        return levels
    }

    /// A side that is not arriving at all: a hair either way of −57 dBFS, which
    /// is well under the −50 taken for silence and still above the −60 the
    /// recorder prints for nothing whatever — a microphone that is muted rather
    /// than a device that has gone. Not flat, because a real dead channel is
    /// not flat, and one perfect line would be a drawing rather than a reading.
    private static func quiet(_ rng: inout SplitMix64) -> [Double] {
        (0..<leastSteps).map { _ in -58 + Double(rng.next(200)) / 100 }
    }
}

/// Reproducible noise, so a preview of one scenario is the same preview every
/// time it is opened. Small on purpose: what is wanted is a stream that does
/// not repeat while it is being watched, not one that would stand up to being
/// counted.
struct SplitMix64 {
    private var seed: UInt64

    init(_ seed: UInt64) {
        self.seed = seed
    }

    mutating func next(_ bound: UInt64) -> UInt64 {
        seed &+= 0x9e37_79b9_7f4a_7c15
        var z = seed
        z = (z ^ (z >> 30)) &* 0xbf58_476d_1ce4_e5b9
        z = (z ^ (z >> 27)) &* 0x94d0_49bb_1331_11eb
        return (z ^ (z >> 31)) % bound
    }
}

/// The words both meters are described in: spoken to a screen reader, rested
/// under a pointer, and written along the bottom of the panel. Phrasing only,
/// and no pixels — what a recording looks like is the panel's own drawing, and
/// what it sounds like has to be sayable without any of that being seen.
enum Meters {
    /// Silence is worth mentioning after ten seconds and not before. Pauses
    /// are ordinary in a meeting — somebody reading a slide, a question being
    /// thought about — and a warning that fires on all of them is a warning
    /// that gets ignored on the one that means a muted microphone.
    static let silenceAfter: TimeInterval = 10

    /// How one side is said out loud: how loud it is, or how long it has been
    /// silent. A screen reader gets no waveform at all, so the sentence has to
    /// carry the thing the shape of a flatline carries — that this side is not
    /// merely quiet, it has been quiet for long enough to be broken.
    static func spoken(_ side: String, _ history: LevelHistory, now: Date = Date()) -> String {
        if let quiet = history.silentFor(now: now) {
            return quiet < 1 ? "\(side) silent" : "\(side) silent for \(lasting(quiet))"
        }
        let level = Int(history.latest.rounded())
        return level >= 0 ? "\(side) 0 dB" : "\(side) −\(-level) dB"
    }

    /// The line under the meters in the panel, which always has something to
    /// say: how long a side has been silent, or else that both of them are
    /// being heard. Never blank, because the panel cannot change height while
    /// somebody is watching it and so the line is standing there either way —
    /// and a line standing there with nothing on it reads as something broken
    /// rather than as nothing to report.
    ///
    /// It is also the whole of what this app says about silence. There is no
    /// notification: a pause is not an event, and being interrupted in the
    /// middle of a meeting is worse than what the interruption would be about.
    static func footer(
        system: LevelHistory, microphone: LevelHistory, now: Date = Date()
    ) -> String {
        switch (worthSaying(system, now), worthSaying(microphone, now)) {
        case let (.some(quietSystem), .some(quietMicrophone)):
            // the shorter of the two, because that is the span both sides have
            // been silent for and the sentence has to be true of both
            return "Both sides silent for \(lasting(min(quietSystem, quietMicrophone)))"
        case let (.some(quietSystem), .none):
            return "System audio silent for \(lasting(quietSystem))"
        case let (.none, .some(quietMicrophone)):
            return "Microphone silent for \(lasting(quietMicrophone))"
        case (.none, .none):
            return "Sound arriving on both sides"
        }
    }

    /// How long a side has been silent, once that is long enough to be worth
    /// a line of its own.
    private static func worthSaying(_ history: LevelHistory, _ now: Date) -> TimeInterval? {
        guard let quiet = history.silentFor(now: now), quiet >= silenceAfter else { return nil }
        return quiet
    }

    /// A span of silence in words: seconds while there are few enough of them
    /// to mean anything, minutes after that, because "silent for 214 s" is a
    /// number to be worked out rather than a thing to be read.
    private static func lasting(_ seconds: TimeInterval) -> String {
        if seconds < 60 {
            return "\(Int(seconds)) s"
        }
        return "\(Int(seconds / 60)) min"
    }
}

/// How a column of the panel's waveform is shaped, and how loud a reading has
/// to be to make one. It lives apart from the view that draws it because the
/// number of columns is the one thing outside the drawing that depends on the
/// panel's width: it is how many readings a history has to keep.
enum Waveform {
    /// Three points of bar, three of air, and a rounded cap on each end. The
    /// pitch is what decides whether a talking voice reads as syllables or as a
    /// fence, and at six points it is syllables: one reading is 100 ms of room,
    /// and six points is wide enough that a single one of them is a bar
    /// somebody can watch rise and fall. Narrower fits more of the meeting into
    /// the tray and shows less of it — a room at ordinary loudness drawn at a
    /// four point pitch is a comb of near-equal teeth, which says a level is
    /// arriving and nothing whatever about what it is doing.
    static let barWidth: CGFloat = 3
    static let gap: CGFloat = 3
    static let pitch: CGFloat = barWidth + gap
    static let cap: CGFloat = 1.5

    /// The tray the bars sit in: rounded, barely lighter than the panel behind
    /// it, and padded at both ends so the oldest and the newest column are not
    /// pressed against its edges.
    static let height: CGFloat = 36
    static let corner: CGFloat = 10
    static let padding: CGFloat = 10

    /// The tallest a bar may be and the shortest it may be while still being a
    /// bar. The air above and below the tallest is what keeps a loud passage
    /// from looking clipped by the tray rather than loud.
    static let breathing: CGFloat = 4
    static let tallest: CGFloat = height - breathing * 2
    static let shortest: CGFloat = 2

    /// The track the newest reading fills under Reduce Motion, in place of the
    /// bars. Thin enough that an empty one is plainly a container waiting to be
    /// filled rather than a signal that has gone flat.
    static let trackHeight: CGFloat = 4

    /// How wide the bars have to fit, and so how many of them there are. At ten
    /// readings a second the panel holds a little over four seconds of meeting —
    /// long enough to see a phrase in, short enough that what is on screen is
    /// still what is happening. Widening a bar spends history to buy legibility,
    /// and four seconds of a meeting somebody can read beats six they cannot.
    static var span: CGFloat { RecordingPanelView.contentWidth - padding * 2 }
    static let columns = Int(span / pitch)

    /// Which whole even height a reading lands on, over −50 dBFS to 0, with
    /// everything below that being silence and having a shape of its own.
    ///
    /// Even for the same reason the mark's bars are even: a bar is mirrored
    /// around the middle of the tray, so an odd height puts both of its ends on
    /// a half point, and a half-point edge is a grey smear rather than a line
    /// on a display that is not retina.
    static func barHeight(for level: Double) -> CGFloat {
        let range = LevelHistory.ceiling - LevelHistory.silence
        let loud = (min(level, LevelHistory.ceiling) - LevelHistory.silence) / range
        let raw = shortest + (tallest - shortest) * CGFloat(loud)
        return min(max((raw / 2).rounded() * 2, shortest), tallest)
    }
}

/// One side of a recording drawn as the waveform a tape machine shows: oldest
/// column at the left, the newest against the right edge, every bar mirrored
/// around the middle of the tray. It is the thing the panel is opened for, and
/// the reason it is a waveform rather than a level meter is that a meter says
/// how loud this instant is and a waveform says whether a voice has been
/// arriving — which is the question somebody mid-meeting is actually asking.
///
/// The tray and the bars are drawn rather than layered so that this renders the
/// same offscreen as it does on screen, which is the only way the look of a
/// recording can be judged without taping one first.
final class WaveformView: NSView {
    /// The oldest column is a ghost and the newest is nearly solid, so which
    /// end of the tray is now needs no explaining.
    private static let oldest: CGFloat = 0.25
    private static let newest: CGFloat = 0.95

    /// How much of the tray's own light stands out from the panel, and how
    /// bright the Reduce Motion track and its fill are.
    private static let trayInk: CGFloat = 0.08
    private static let trackInk: CGFloat = 0.1
    private static let fillInk: CGFloat = 0.9

    /// Twice a second is as often as a track that only changes length is worth
    /// redrawing, and it is what Reduce Motion is asking for.
    private static let slowly: TimeInterval = 0.5

    private let reduceMotion: Bool
    private var trace: [Double] = []
    private var drawnAt = Date.distantPast

    /// Reduce Motion is settled when the view is built and not looked at again.
    /// A panel lives for as long as somebody keeps it open, and a waveform that
    /// changed shape halfway through being watched would be a stranger thing
    /// than either of the two shapes it changed between.
    init(reduceMotion: Bool) {
        self.reduceMotion = reduceMotion
        super.init(frame: NSRect(
            x: 0, y: 0, width: RecordingPanelView.contentWidth, height: Waveform.height
        ))
        // there is nothing here for a screen reader to read: the caption above
        // says which side this is and the footer below says in words whether it
        // is being heard, which is the whole of what the shape carries
        setAccessibilityElement(false)
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("WaveformView is built in code, not loaded from a nib")
    }

    /// The readings copied out, and a redraw asked for. Copied rather than the
    /// history kept, for the reason the history itself gives: drawing happens
    /// later than this does, and by then the next reading has landed.
    ///
    /// Under Reduce Motion the copy still happens and only the redraw is held
    /// back, so the track shows the level as it is at the moment it is drawn
    /// rather than as it was half a second ago.
    func show(_ history: LevelHistory, now: Date = Date()) {
        trace = history.trace
        if reduceMotion, now.timeIntervalSince(drawnAt) < WaveformView.slowly {
            return
        }
        drawnAt = now
        needsDisplay = true
    }

    /// The tray first and then whatever is in it, in one pass rather than as a
    /// tray view with a waveform view inside it: the two are always drawn at
    /// the same moment and the tray is three lines of drawing, where a second
    /// view would be a second frame to keep in step with this one.
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(WaveformView.trayInk).setFill()
        NSBezierPath(
            roundedRect: bounds, xRadius: Waveform.corner, yRadius: Waveform.corner
        ).fill()
        guard let newest = trace.last else { return }
        // whole point, and the tray is an even number of points tall, so every
        // mirrored bar has both of its ends on a whole point too
        let midline = (bounds.height / 2).rounded()
        if reduceMotion {
            fill(newest, midline: midline)
            return
        }
        let shown = trace.suffix(Waveform.columns)
        let last = CGFloat(max(shown.count - 1, 1))
        for (column, level) in shown.enumerated() {
            let age = CGFloat(column) / last
            bar(
                level, at: Waveform.padding + CGFloat(column) * Waveform.pitch, midline: midline,
                ink: WaveformView.oldest + (WaveformView.newest - WaveformView.oldest) * age
            )
        }
    }

    /// One reading, mirrored around the middle of the tray. Silence is not the
    /// shortest bar but a flat line: a difference in shape, which somebody
    /// reads at a glance and still reads with the colour taken away, where a
    /// two-point bar beside a four-point one is a guess.
    ///
    /// The line is drawn the full width of its column rather than the width of
    /// a bar, so neighbouring silent readings meet and make one line — a row of
    /// dots at the spacing of the bars is a texture, and the shape somebody has
    /// to recognise here is a flatline. It keeps its column's own ink rather
    /// than being dimmed any further: the tray is already a lighter ground than
    /// the panel around it, and a dimmer line disappears into it.
    private func bar(_ level: Double, at left: CGFloat, midline: CGFloat, ink: CGFloat) {
        NSColor.labelColor.withAlphaComponent(ink).setFill()
        guard level >= LevelHistory.silence else {
            // a whole point under the midline rather than astride it, since a
            // one-point line centred there lands half in each of two rows of
            // pixels and comes out grey
            NSRect(x: left, y: midline - 1, width: Waveform.pitch, height: 1).fill()
            return
        }
        let height = Waveform.barHeight(for: level)
        let rect = NSRect(
            x: left, y: midline - height / 2, width: Waveform.barWidth, height: height
        )
        NSBezierPath(roundedRect: rect, xRadius: Waveform.cap, yRadius: Waveform.cap).fill()
    }

    /// The newest reading as a track filling from the left, which is what
    /// Reduce Motion leaves room for: nothing scrolls past, no column changes
    /// ten times a second, and the one thing a meter exists to show — how loud
    /// this side is — is still there to be read. The scale is the whole of it,
    /// floor to ceiling, because a fill has no shape to say anything with and
    /// its length is all there is.
    ///
    /// Silence empties the track rather than shortening it, and the track is
    /// drawn either way: an empty container says nothing is arriving, where a
    /// missing one would only look like a meter that failed to draw.
    private func fill(_ level: Double, midline: CGFloat) {
        let cap = Waveform.trackHeight / 2
        let track = NSRect(
            x: Waveform.padding, y: midline - cap, width: Waveform.span,
            height: Waveform.trackHeight
        )
        NSColor.labelColor.withAlphaComponent(WaveformView.trackInk).setFill()
        NSBezierPath(roundedRect: track, xRadius: cap, yRadius: cap).fill()
        guard level >= LevelHistory.silence else { return }
        let range = LevelHistory.ceiling - LevelHistory.floor
        let loud = (min(level, LevelHistory.ceiling) - LevelHistory.floor) / range
        NSColor.labelColor.withAlphaComponent(WaveformView.fillInk).setFill()
        // rounded to a whole point, for the same reason every other edge here
        // is, and never shorter than its own cap, or a quiet reading draws a
        // rounded rectangle narrower than its corners and comes out a smudge
        let width = max((CGFloat(loud) * track.width).rounded(), Waveform.trackHeight)
        NSBezierPath(
            roundedRect: NSRect(
                x: track.minX, y: track.minY, width: width, height: track.height
            ), xRadius: cap, yRadius: cap
        ).fill()
    }
}

/// The red dot at the head of the panel, with a halo breathing slowly behind
/// it. Red alone already says a capture is live — it is the colour macOS uses
/// for one — and the halo is there for the glance that catches the panel out of
/// the corner of an eye: a thing that moves is a thing that is happening now,
/// where a still dot could as easily be a picture of one.
///
/// Slow on purpose. A beat over a second is breathing; anything quicker is an
/// alarm, and nothing here is going wrong.
final class RecordingDotView: NSView {
    static let side: CGFloat = 8

    /// How far the halo swells, how bright it is at each end of its breath, and
    /// how long half a breath takes — the animation reverses, so the cycle is
    /// twice this.
    private static let swell: CGFloat = 2.2
    private static let brightest: Float = 0.45
    private static let faintest: Float = 0.12
    private static let halfBeat: CFTimeInterval = 0.6

    private let pulsing: Bool
    private let halo = CALayer()

    /// Reduce Motion settles at the moment the panel is built rather than at
    /// launch, because somebody can turn it on between one meeting and the next
    /// and the recording after that is the one it should apply to.
    init(pulsing: Bool) {
        self.pulsing = pulsing
        super.init(frame: NSRect(
            x: 0, y: 0, width: RecordingDotView.side, height: RecordingDotView.side
        ))
        wantsLayer = true
        halo.frame = bounds
        halo.cornerRadius = RecordingDotView.side / 2
        halo.backgroundColor = NSColor.systemRed.cgColor
        halo.opacity = 0
        // the halo composites over the dot rather than under it, which a
        // sublayer has no choice about, and it makes no difference: both are the
        // same red and the dot beneath is opaque, so a tint of itself laid over
        // it is still the dot. What swells past its edge is the whole effect
        layer?.addSublayer(halo)
        setAccessibilityElement(false)
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("RecordingDotView is built in code, not loaded from a nib")
    }

    /// The dot itself is drawn rather than layered, so that it is there in
    /// every rendering of the panel including the ones made offscreen, where a
    /// layer's contents never arrive. Only the halo needs to be a layer, and
    /// only because a still picture is not what it is for.
    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemRed.setFill()
        NSBezierPath(ovalIn: bounds).fill()
    }

    /// The breath is tied to being on screen rather than started once, because
    /// a panel is closed and opened again all through a meeting and Core
    /// Animation is under no obligation to keep an animation on a layer that
    /// has been out of a window in between.
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        halo.removeAllAnimations()
        guard pulsing, window != nil else {
            halo.opacity = 0
            return
        }
        halo.opacity = RecordingDotView.faintest
        halo.add(breathe(#keyPath(CALayer.opacity),
            from: RecordingDotView.brightest, to: RecordingDotView.faintest), forKey: "glow")
        halo.add(breathe("transform.scale",
            from: 1, to: Float(RecordingDotView.swell)), forKey: "swell")
    }

    /// Out and back on an eased curve rather than a linear one, so the halo
    /// slows at both ends of its breath instead of snapping around at them.
    private func breathe(_ path: String, from: Float, to: Float) -> CABasicAnimation {
        let animation = CABasicAnimation(keyPath: path)
        animation.fromValue = from
        animation.toValue = to
        animation.duration = RecordingDotView.halfBeat
        animation.autoreverses = true
        animation.repeatCount = .infinity
        animation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        return animation
    }
}

/// The one thing in the panel that does anything, drawn as a filled capsule
/// rather than left to AppKit. A bordered mac button on a forced-dark panel is
/// a grey slab in the system's own materials — it reads as disabled, which is
/// the opposite of what the only action on screen should read as.
///
/// So the capsule is drawn here and the button is left to draw its title, which
/// is also what keeps its accessibility title being its title.
final class HUDStopButton: NSButton {
    /// How far the fill drops while it is held down. Enough to feel pressed,
    /// not so much that it looks like a different colour.
    private static let pressed: CGFloat = 0.7

    /// The title is set as attributed text because that is the only way to say
    /// what colour it is: a plain title on a borderless button is drawn in the
    /// control colour of the appearance, which over a red capsule is the one
    /// colour it must not be.
    init(title: String) {
        super.init(frame: .zero)
        isBordered = false
        setButtonType(.momentaryChange)
        let centred = NSMutableParagraphStyle()
        centred.alignment = .center
        attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
            .foregroundColor: NSColor.white,
            .paragraphStyle: centred,
        ])
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("HUDStopButton is built in code, not loaded from a nib")
    }

    /// The capsule under the title. Super is called after it rather than
    /// instead of it, so the button goes on drawing its own title and its own
    /// pressed state and this adds only the shape it is sitting on.
    override func draw(_ dirtyRect: NSRect) {
        let cap = bounds.height / 2
        NSColor.systemRed.withAlphaComponent(isHighlighted ? HUDStopButton.pressed : 1).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: cap, yRadius: cap).fill()
        super.draw(dirtyRect)
    }
}

/// The recording as a window of its own: a small dark island hanging from the
/// menu bar, with both sides of the tape drawn in it and one button. It exists
/// because a menu cannot be watched — it closes on the first click anywhere,
/// including the click that was meant to unmute somebody — and watching is the
/// entire reason to look at a meter mid-meeting.
///
/// Always dark, whatever the Mac is set to. This floats over a meeting rather
/// than sitting in a document, and every app that puts a control layer over a
/// call draws it dark: matching the desktop instead would make it a window that
/// had wandered in from another task.
///
/// Dark and solid, rather than dark and translucent. A vibrancy material takes
/// its colour from whatever is behind it, and behind this is somebody's
/// desktop picture: over a warm one the island turns muddy brown, which is a
/// window looking broken rather than looking like itself. One painted colour is
/// also what makes a rendering of this panel made offscreen the same pixels as
/// the panel on screen, so its look can be judged without taping a meeting.
///
/// The frames are set by hand rather than constrained. Nothing in here may
/// change size while somebody is watching it — a silence coming on, a device
/// with a longer name, a timer crossing an hour — because a panel that resizes
/// under the pointer is the one kind of movement there is no reading through.
/// That is also why the footer is a line that is always there and always says
/// something.
final class RecordingPanelView: NSView {
    /// Wide enough for a device name to survive beside its caption, narrow
    /// enough to hang off a status item without covering the menu bar's own
    /// items either side of it.
    static let width: CGFloat = 300
    static let inset: CGFloat = 16
    static var contentWidth: CGFloat { width - inset * 2 }

    /// Rounded on all four corners, the way a detached popover is. This hangs
    /// clear of the menu bar rather than pressing against it: an opaque dark
    /// island flush under a light translucent bar meets it at a square corner
    /// with nothing to hide the join, and a square corner among rounded ones
    /// reads as a thing that failed to draw rather than as a thing joined on.
    static let corner: CGFloat = 16

    /// The one colour the island is painted. Warm rather than a neutral black,
    /// which is what keeps it from reading as a hole cut in the desktop, and
    /// dark enough that the white of the header is the brightest thing in it.
    private static let ground = NSColor(
        srgbRed: 30 / 255, green: 30 / 255, blue: 33 / 255, alpha: 1
    )

    /// How long the window takes to fade up, how long the island inside it
    /// takes to grow into place, how long it all takes to go, and how small the
    /// island starts. The fade is short because this panel stands in for a
    /// menu, and a menu is on screen when it is asked for; the third of a
    /// second is the island settling, which is the part that says this window
    /// came out of the status item above it.
    ///
    /// The fade has to be over while the growth is still moving. Run the two on
    /// one duration and the panel spends its opening frames too faint to read,
    /// and by the time there is anything to read the growth has all but
    /// finished — a movement that happens correctly and is never seen.
    ///
    /// Eight per cent is still short of a zoom — twenty-four points across a
    /// panel this wide — because the thing being shown is where the window came
    /// from, not that it made an entrance. Going is the fade and nothing else:
    /// a window that takes as long to leave is a window standing in the way of
    /// whatever dismissed it.
    static let fadingUp: TimeInterval = 0.12
    static let appearing: TimeInterval = 0.30
    static let vanishing: TimeInterval = 0.12
    private static let arrivingFrom: CGFloat = 0.92

    /// The curve both movements run on. Ease-out rather than something slow at
    /// both ends: this island stands in for a menu, and a menu is on screen the
    /// instant it is asked for. A curve that eased in as well would spend its
    /// first fifty milliseconds invisible, which on a panel opened by a click
    /// is not calm but slow. So the movement happens where it is looked at and
    /// then settles.
    static var easing: CAMediaTimingFunction {
        CAMediaTimingFunction(name: .easeOut)
    }

    /// A row deep enough for 13 point type, and the air under it, which is
    /// wider than the air between the meters because the header is a different
    /// kind of thing from what is under it.
    private static let headerHeight: CGFloat = 18
    private static let headerGap: CGFloat = 14

    /// A caption belongs to the meter under it, so it sits nearer to that than
    /// the rows sit to each other.
    private static let captionHeight: CGFloat = 14
    private static let tight: CGFloat = 2
    private static let spacing: CGFloat = 12

    /// A touch under the 32 points a full-width button gets in a sheet: this is
    /// a HUD hanging in the air, and the same button at the same size would
    /// weigh the whole island down.
    private static let buttonHeight: CGFloat = 30

    /// The air after the dot, and the room kept for the timer. The timer's
    /// width is fixed rather than fitted so that crossing an hour moves
    /// nothing: it is monospaced digits in a right-aligned field, and the field
    /// is as wide as the longest thing that can land in it.
    private static let dotGap: CGFloat = 8
    private static let timerWidth: CGFloat = 90

    /// Fixed, like the width: a header, two captioned meters, the footer and
    /// the button, with the insets top and bottom.
    static var height: CGFloat {
        inset * 2 + headerHeight + headerGap
            + (captionHeight + tight + Waveform.height + spacing) * 2
            + captionHeight + spacing + buttonHeight
    }

    private let dot: RecordingDotView
    private let title = RecordingPanelView.heading("Recording")
    private let timer = RecordingPanelView.digits()
    private let systemCaption: NSTextField
    private let systemWave: WaveformView
    private let microphoneCaption: NSTextField
    private let microphoneWave: WaveformView
    private let footer = RecordingPanelView.caption("")
    private let stop = HUDStopButton(title: "Stop Recording")

    /// Settled when the panel is built and then kept, because everything that
    /// moves in here — the halo, the two waveforms, the island's own arrival —
    /// has to agree about it for the whole life of one panel.
    private let reduceMotion: Bool

    /// The pointer having arrived inside the island. It is set from outside and
    /// this view neither knows nor cares what it is taken to mean: what it is
    /// for is a window that opened itself becoming a window somebody is
    /// reading, and that is a thing about recordings rather than about drawing.
    var onEngage: (() -> Void)?

    /// Where the pointer is watched for, kept so that the one before it can be
    /// taken off whenever AppKit asks for these to be rebuilt.
    private var pointer: NSTrackingArea?

    /// The devices are taken at the start of a recording and never asked for
    /// again: they are what this recording was started with, and a device
    /// picked afterwards changes nothing about the tape running.
    init(system: String, microphone: String, reduceMotion: Bool) {
        self.reduceMotion = reduceMotion
        dot = RecordingDotView(pulsing: !reduceMotion)
        systemCaption = RecordingPanelView.caption("System audio · \(system)")
        systemWave = WaveformView(reduceMotion: reduceMotion)
        microphoneCaption = RecordingPanelView.caption("Microphone · \(microphone)")
        microphoneWave = WaveformView(reduceMotion: reduceMotion)
        super.init(frame: NSRect(
            x: 0, y: 0, width: RecordingPanelView.width, height: RecordingPanelView.height
        ))
        // forced dark rather than merely painted dark: every label colour in
        // here resolves against the appearance the view is wearing, and one
        // that followed the Mac would put black type on this ground the moment
        // somebody switched their Mac to light
        appearance = NSAppearance(named: .darkAqua)
        wantsLayer = true
        layer?.backgroundColor = RecordingPanelView.ground.cgColor
        layer?.cornerRadius = RecordingPanelView.corner
        layer?.masksToBounds = true
        setAccessibilityRole(.group)
        setAccessibilityLabel("Recording")
        layOut()
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("RecordingPanelView is built in code, not loaded from a nib")
    }

    /// Everything placed from the top down, which is the order it is read in
    /// and the only order these measurements make sense in.
    private func layOut() {
        var top = RecordingPanelView.height - RecordingPanelView.inset
        dot.frame = NSRect(
            x: RecordingPanelView.inset,
            y: top - RecordingPanelView.headerHeight / 2 - RecordingDotView.side / 2,
            width: RecordingDotView.side, height: RecordingDotView.side
        )
        let afterDot = RecordingDotView.side + RecordingPanelView.dotGap
        title.frame = NSRect(
            x: RecordingPanelView.inset + afterDot, y: top - RecordingPanelView.headerHeight,
            width: RecordingPanelView.contentWidth - afterDot - RecordingPanelView.timerWidth
                - RecordingPanelView.dotGap,
            height: RecordingPanelView.headerHeight
        )
        timer.frame = NSRect(
            x: RecordingPanelView.inset + RecordingPanelView.contentWidth
                - RecordingPanelView.timerWidth,
            y: top - RecordingPanelView.headerHeight,
            width: RecordingPanelView.timerWidth, height: RecordingPanelView.headerHeight
        )
        top -= RecordingPanelView.headerHeight + RecordingPanelView.headerGap
        for (caption, wave) in [
            (systemCaption, systemWave), (microphoneCaption, microphoneWave),
        ] {
            caption.frame = row(top, RecordingPanelView.captionHeight)
            top -= RecordingPanelView.captionHeight + RecordingPanelView.tight
            wave.frame = row(top, Waveform.height)
            top -= Waveform.height + RecordingPanelView.spacing
            addSubview(caption)
            addSubview(wave)
        }
        footer.frame = row(top, RecordingPanelView.captionHeight)
        top -= RecordingPanelView.captionHeight + RecordingPanelView.spacing
        stop.frame = row(top, RecordingPanelView.buttonHeight)
        for view in [dot, title, timer, footer, stop] as [NSView] {
            addSubview(view)
        }
    }

    /// A full-width row of the given height hanging from `top`.
    private func row(_ top: CGFloat, _ height: CGFloat) -> NSRect {
        NSRect(
            x: RecordingPanelView.inset, y: top - height,
            width: RecordingPanelView.contentWidth, height: height
        )
    }

    /// The island grown into place rather than switched on: it starts a shade
    /// small and pinned by its top edge, so what the eye follows is the bottom
    /// of it coming down out of the status item it hangs from.
    ///
    /// Nothing is left set on the layer. The small size rides on the animation
    /// and goes when the animation is taken off, so what the panel rests at is
    /// the identity every measurement in here is written against — and a second
    /// arrival starts from the same place the first one did.
    func appear() {
        guard !reduceMotion, let layer else { return }
        let growing = CABasicAnimation(keyPath: "transform")
        growing.fromValue = NSValue(caTransform3D: RecordingPanelView.scaled(
            RecordingPanelView.arrivingFrom, about: layer.bounds
        ))
        growing.toValue = NSValue(caTransform3D: CATransform3DIdentity)
        growing.duration = RecordingPanelView.appearing
        growing.timingFunction = RecordingPanelView.easing
        layer.add(growing, forKey: "appear")
    }

    /// A scale held at the top edge of a layer instead of at its middle,
    /// written as a translation either side of the scale. Moving the layer's
    /// own `anchorPoint` says the same thing and shifts the layer half its
    /// height the instant it is set, which then has to be taken back out by
    /// moving `position` to match — two corrections that have to agree, where
    /// this is one expression that cannot disagree with itself.
    ///
    /// This view is unflipped, so the top edge is half the height above the
    /// middle the transform turns about.
    static func scaled(_ scale: CGFloat, about bounds: CGRect) -> CATransform3D {
        var transform = CATransform3DIdentity
        transform = CATransform3DTranslate(transform, 0, bounds.height / 2, 0)
        transform = CATransform3DScale(transform, scale, scale, 1)
        transform = CATransform3DTranslate(transform, 0, -bounds.height / 2, 0)
        return transform
    }

    /// The pointer coming inside the island is a different thing from the
    /// window merely being on screen, and this is what tells the two apart. A
    /// tracking area rather than mouse-moved events because this panel never
    /// becomes key and never activates the app: `.activeAlways` is what gets a
    /// crossing reported to a window nobody has clicked into.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let pointer {
            removeTrackingArea(pointer)
        }
        let area = NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil
        )
        addTrackingArea(area)
        pointer = area
    }

    /// Only the arrival is passed on. Leaving again is not the opposite of
    /// having engaged with the window: somebody who has moved a pointer across
    /// it has still read it, and a panel that started closing itself again on
    /// the way out would be doing the very thing being told about the arrival
    /// is meant to prevent.
    override func mouseEntered(with event: NSEvent) {
        onEngage?()
    }

    /// Both sides, as often as readings arrive. This is the whole point of the
    /// panel being a window rather than a menu: it goes on moving while
    /// somebody watches it, and nothing they do elsewhere interrupts it.
    func show(system: LevelHistory, microphone: LevelHistory, now: Date = Date()) {
        systemWave.show(system, now: now)
        microphoneWave.show(microphone, now: now)
    }

    /// How far into the recording it is, once a second, which is as often as
    /// anything in it can change. It is also what the panel says to a screen
    /// reader: the loudness of each side is already spoken on the status item,
    /// and what this window adds is that a recording is running and how long it
    /// has been.
    func showHeading(_ text: String) {
        if title.stringValue != text {
            title.stringValue = text
        }
    }

    func showElapsed(_ text: String) {
        timer.stringValue = text
        setAccessibilityLabel("Recording, \(text)")
    }

    /// The line under the meters, refreshed with the timer rather than with
    /// every reading: it counts in whole seconds, and nothing in it can change
    /// ten times between two of them.
    func showFooter(system: LevelHistory, microphone: LevelHistory, now: Date = Date()) {
        footer.stringValue = Meters.footer(system: system, microphone: microphone, now: now)
    }

    /// What the button does, wired from outside. This view knows that stopping
    /// is a thing somebody can ask for and nothing whatever about what stopping
    /// means, which is what lets the whole look of it be rendered offscreen
    /// with no recorder anywhere near.
    func onStop(_ target: AnyObject, _ action: Selector) {
        stop.target = target
        stop.action = action
    }

    /// What the panel calls itself, in the weight a title takes.
    private static func heading(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        field.textColor = .labelColor
        return field
    }

    /// The clock. Monospaced digits, or every tick would shuffle the numbers
    /// left and right under a pointer resting on them.
    private static func digits() -> NSTextField {
        let field = NSTextField(labelWithString: "")
        field.font = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        field.textColor = .secondaryLabelColor
        field.alignment = .right
        return field
    }

    /// Small, secondary and one line: the type used for what the panel is
    /// saying rather than offering. Truncated at the tail because a device is
    /// recognised from the start of its name — "MacBook Pro Micro…" is still
    /// the built-in microphone.
    private static func caption(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: 11)
        field.textColor = .secondaryLabelColor
        field.maximumNumberOfLines = 1
        field.lineBreakMode = .byTruncatingTail
        return field
    }
}

// MARK: - the dial

/// How long the next recording is to run, as a ring turned rather than a number
/// typed. A meeting has a length before it starts — the invitation says so —
/// and a tape that stops itself at that length is one nobody has to remember
/// to stop with a call still up on the screen. Sixty ticks, two minutes each:
/// as fine as a meeting is ever booked, and as far round as two hours.
///
/// Nothing in here is drawn in `draw(_:)`. The disc, the ring of unlit ticks,
/// the ring of lit ones and the arc that decides which of them show are all
/// layers built once; turning the dial changes one number on one of them
/// (`strokeEnd`) and the render server does the rest, so a drag costs no
/// redraw at all. The ticks are one `CAReplicatorLayer` each rather than sixty
/// layers — one tick, replicated round the circle by a rotation — which is
/// how a clock face is drawn cheaply, and the lit ring is the same replicator
/// again in the accent colour, masked by the arc.
///
/// A drag adds the *shortest signed turn* since the last event to the value,
/// never the pointer's absolute angle: a value taken straight from the angle
/// snaps from two hours to two minutes the instant a drag crosses twelve
/// o'clock, which is the one thing a dial must never do.
final class DialView: NSView, NSAccessibilitySlider {
    static let ticks = 60
    static let step = 2
    static let shortest = step
    static var longest: Int { ticks * step }

    /// The disc, and the air kept round it for its own shadow: the panel it sits
    /// in casts none, because a window shadow on a transparent window is worked
    /// out from the pixels and worked out again every time they change.
    static let disc: CGFloat = 220
    static let margin: CGFloat = 28
    static var side: CGFloat { disc + margin * 2 }

    /// The same warm near-black as the recording island, so the two read as one
    /// app's windows, and a hairline of white at the rim so the disc has an edge
    /// against a dark desktop.
    private static let ground = NSColor(srgbRed: 30 / 255, green: 30 / 255, blue: 33 / 255, alpha: 1)
    private static let rim = NSColor.white.withAlphaComponent(0.09)
    private static let unlit = NSColor.white.withAlphaComponent(0.2)
    private static let lit = NSColor.systemGreen

    private static let tickLength: CGFloat = 11
    private static let tickWidth: CGFloat = 3
    private static let ringInset: CGFloat = 16
    private static let centreDiameter: CGFloat = 96

    /// How far a scroll wheel travels for one tick. A notched wheel sends about
    /// ten points a notch, so this is one notch, one tick, one pulse.
    private static let scrollPerTick: CGFloat = 10

    private let disc = CAShapeLayer()
    private let unlitTicks = CAReplicatorLayer()
    private let litTicks = CAReplicatorLayer()
    private let litArc = CAShapeLayer()
    private let centre = DialCentreButton()

    /// What the dial is set to, and the unrounded turn underneath it. The
    /// turn is what a drag moves; the minutes are the turn rounded to a tick,
    /// and only the minutes ever reach the layers or the person.
    private(set) var minutes: Int
    private var turn: CGFloat
    private var lastAngle: CGFloat?
    private var scrolled: CGFloat = 0
    private let reduceMotion: Bool

    var onStart: ((Int) -> Void)?
    var onChange: ((Int) -> Void)?

    /// A press on the dial's ground — inside the ring, or the air around the
    /// disc — is a hand on the window rather than on the control, and this is
    /// how the view says so, in screen points, from the press to the release.
    /// What a grip *does* is not the view's business.
    enum Grip {
        case began(NSPoint)
        case moved(NSPoint)
        case ended(NSPoint)
    }

    var onGrip: ((Grip) -> Void)?

    /// The pointer crossing the dial's edge, in or out. One tracking area,
    /// entered and exited only: mouse-moved events flood the queue and are
    /// not asked for, and nothing here needs to know where the pointer is
    /// between one crossing and the next.
    var onHover: ((Bool) -> Void)?

    private var gripping = false
    private var pointer: NSTrackingArea?

    init(minutes: Int, reduceMotion: Bool) {
        self.minutes = DialView.clamped(minutes)
        self.turn = CGFloat(self.minutes)
        self.reduceMotion = reduceMotion
        super.init(frame: NSRect(x: 0, y: 0, width: DialView.side, height: DialView.side))
        appearance = NSAppearance(named: .darkAqua)
        wantsLayer = true
        // nothing here is drawn by this view, so nothing here wants redrawing
        // when the window resizes — and a policy that redrew anyway would be
        // work done for pixels that never change
        layerContentsRedrawPolicy = .onSetNeedsDisplay
        buildLayers()
        centre.frame = NSRect(
            x: bounds.midX - DialView.centreDiameter / 2,
            y: bounds.midY - DialView.centreDiameter / 2,
            width: DialView.centreDiameter, height: DialView.centreDiameter
        )
        centre.target = self
        centre.action = #selector(start)
        addSubview(centre)
        setAccessibilityLabel("Recording length")
        show(animated: false)
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("DialView is built in code, not loaded from a nib")
    }

    // --- the layers ----------------------------------------------------------

    private var discRect: NSRect {
        NSRect(x: DialView.margin, y: DialView.margin, width: DialView.disc, height: DialView.disc)
    }

    private func buildLayers() {
        guard let layer else { return }
        let round = CGPath(ellipseIn: discRect.insetBy(dx: 0.5, dy: 0.5), transform: nil)
        disc.path = round
        disc.fillColor = DialView.ground.cgColor
        disc.strokeColor = DialView.rim.cgColor
        disc.lineWidth = 1
        // the shadow's shape given rather than found: a layer whose shadow is
        // read off its pixels has it read again on every change, and this one
        // never changes shape
        disc.shadowPath = round
        disc.shadowColor = NSColor.black.cgColor
        disc.shadowOpacity = 0.55
        disc.shadowRadius = 14
        disc.shadowOffset = CGSize(width: 0, height: -5)
        layer.addSublayer(disc)

        for (ring, colour, glowing) in [
            (unlitTicks, DialView.unlit, false), (litTicks, DialView.lit, true),
        ] {
            ring.frame = discRect
            ring.instanceCount = DialView.ticks
            // a negative turn is clockwise on a Mac's unflipped y axis, which is
            // the way a dial and a clock both go
            ring.instanceTransform = CATransform3DMakeRotation(
                -2 * .pi / CGFloat(DialView.ticks), 0, 0, 1
            )
            ring.addSublayer(DialView.tick(colour, glowing: glowing))
            layer.addSublayer(ring)
        }

        // the arc that decides which ticks are lit: a stroke round the ring,
        // as wide as a tick and its glow, run from twelve o'clock clockwise.
        // The path is a polyline made once — its own points say which way
        // round it goes, where an arc primitive says so in terms of a
        // coordinate system somebody has to remember is upside down
        litArc.frame = CGRect(origin: .zero, size: discRect.size)
        litArc.path = DialView.ringPath(
            centre: CGPoint(x: DialView.disc / 2, y: DialView.disc / 2),
            radius: DialView.disc / 2 - DialView.ringInset - DialView.tickLength / 2
        )
        litArc.fillColor = nil
        litArc.strokeColor = NSColor.black.cgColor
        litArc.lineWidth = DialView.tickLength + 14
        litArc.lineCap = .butt
        litArc.strokeStart = 0
        litTicks.mask = litArc
    }

    /// One tick, standing at twelve o'clock; the replicator turns it round the
    /// rest of the face. A lit one glows, and the glow's shape is its own
    /// rounded rectangle given outright, for the same reason the disc's
    /// shadow is: one tick, once, then replicated.
    private static func tick(_ colour: NSColor, glowing: Bool) -> CALayer {
        let tick = CALayer()
        tick.frame = CGRect(
            x: disc / 2 - tickWidth / 2, y: disc - ringInset - tickLength,
            width: tickWidth, height: tickLength
        )
        tick.cornerRadius = tickWidth / 2
        tick.backgroundColor = colour.cgColor
        if glowing {
            tick.shadowPath = CGPath(
                roundedRect: tick.bounds, cornerWidth: tickWidth / 2, cornerHeight: tickWidth / 2,
                transform: nil
            )
            tick.shadowColor = colour.cgColor
            tick.shadowOpacity = 0.9
            tick.shadowRadius = 4
            tick.shadowOffset = .zero
        }
        return tick
    }

    /// A circle as a polyline that starts at the top and goes clockwise, so
    /// that `strokeEnd` at a quarter is a quarter past.
    private static func ringPath(centre: CGPoint, radius: CGFloat) -> CGPath {
        let path = CGMutablePath()
        let points = 240
        for i in 0...points {
            let angle = 2 * CGFloat.pi * CGFloat(i) / CGFloat(points)
            let point = CGPoint(x: centre.x + radius * sin(angle), y: centre.y + radius * cos(angle))
            if i == 0 { path.move(to: point) } else { path.addLine(to: point) }
        }
        return path
    }

    // --- the value ------------------------------------------------------------

    private static func clamped(_ minutes: Int) -> Int {
        min(max(minutes, shortest), longest)
    }

    /// The layers and the readout brought up to the value. The arc moves with
    /// the render server's own quarter second when the change was a click, a
    /// scroll or a key — a jump that eases is a jump that can be followed — and
    /// with none at all under a drag, where the pointer is the animation and a
    /// ring lagging a quarter second behind it would feel like rubber.
    private func show(animated: Bool) {
        let index = minutes / DialView.step
        let fraction = min(1, (CGFloat(index) + 0.5) / CGFloat(DialView.ticks))
        CATransaction.begin()
        CATransaction.setDisableActions(!animated || reduceMotion)
        litArc.strokeEnd = fraction
        CATransaction.commit()
        centre.show(minutes: minutes)
        setAccessibilityValue("\(minutes) minutes")
    }

    /// One new value, from wherever it came. The pulse fires only when the tick
    /// the dial rests on has actually changed — a drag sends dozens of events a
    /// second, and a pulse on each would be a buzz rather than a detent.
    private func settle(_ exact: CGFloat, animated: Bool) {
        turn = min(max(exact, CGFloat(DialView.shortest)), CGFloat(DialView.longest))
        let stepped = DialView.clamped(Int((turn / CGFloat(DialView.step)).rounded()) * DialView.step)
        guard stepped != minutes else { return }
        minutes = stepped
        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .drawCompleted)
        show(animated: animated)
        onChange?(minutes)
    }

    private func step(by ticks: Int) {
        // from the tick the dial rests on, not from the turn underneath it, so
        // that a key press after a drag moves exactly one tick
        turn = CGFloat(minutes)
        settle(turn + CGFloat(ticks * DialView.step), animated: true)
    }

    @objc private func start() {
        onStart?(minutes)
    }

    // --- the pointer ------------------------------------------------------------

    /// Clockwise from twelve o'clock, in radians, for a point in this view.
    private func angle(of point: NSPoint) -> CGFloat {
        let dx = point.x - bounds.midX
        let dy = point.y - bounds.midY
        var angle = atan2(dx, dy)
        if angle < 0 { angle += 2 * .pi }
        return angle
    }

    /// The shorter way round from one angle to the next, signed: clockwise is
    /// positive. This is the whole of what keeps the seam at twelve o'clock
    /// from being a cliff.
    private static func turn(from old: CGFloat, to new: CGFloat) -> CGFloat {
        var delta = new - old
        if delta > .pi { delta -= 2 * .pi } else if delta < -.pi { delta += 2 * .pi }
        return delta
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    /// Where the ring is, as a band of reach from the centre: a press in it
    /// turns the dial, a press anywhere else on the disc or in the air around
    /// it takes hold of the window.
    private static var ringBand: ClosedRange<CGFloat> {
        (disc / 2 - ringInset - tickLength - 10)...(disc / 2 + 4)
    }

    /// A press on the ring sets the dial to where it was pressed and the drag
    /// that follows turns it from there; a press on the ground is a grip.
    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let reach = hypot(point.x - bounds.midX, point.y - bounds.midY)
        guard DialView.ringBand.contains(reach) else {
            gripping = true
            onGrip?(.began(NSEvent.mouseLocation))
            return
        }
        let angle = angle(of: point)
        let ticks = (angle / (2 * .pi) * CGFloat(DialView.ticks)).rounded()
        settle(ticks * CGFloat(DialView.step), animated: true)
        lastAngle = angle
    }

    override func mouseDragged(with event: NSEvent) {
        if gripping {
            onGrip?(.moved(NSEvent.mouseLocation))
            return
        }
        guard let lastAngle else { return }
        let angle = angle(of: convert(event.locationInWindow, from: nil))
        let delta = DialView.turn(from: lastAngle, to: angle)
        self.lastAngle = angle
        settle(turn + delta / (2 * .pi) * CGFloat(DialView.longest), animated: false)
    }

    override func mouseUp(with event: NSEvent) {
        lastAngle = nil
        if gripping {
            gripping = false
            onGrip?(.ended(NSEvent.mouseLocation))
        }
    }

    /// `.activeAlways`, because this window never becomes key and the app it
    /// belongs to is never the active one: any narrower scope would report no
    /// crossing at all. The whole view is the area — the margin round the disc
    /// is the slack that keeps a pointer just off the rim from counting as
    /// gone.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let pointer {
            removeTrackingArea(pointer)
        }
        let area = NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil
        )
        addTrackingArea(area)
        pointer = area
    }

    override func mouseEntered(with event: NSEvent) {
        onHover?(true)
    }

    override func mouseExited(with event: NSEvent) {
        onHover?(false)
    }

    /// A wheel or a two-finger scroll turns the dial too, a tick a notch.
    override func scrollWheel(with event: NSEvent) {
        scrolled += event.scrollingDeltaY
        let ticks = Int(scrolled / DialView.scrollPerTick)
        guard ticks != 0 else { return }
        scrolled -= CGFloat(ticks) * DialView.scrollPerTick
        step(by: -ticks)
    }

    // --- the keyboard and the screen reader ----------------------------------

    override var acceptsFirstResponder: Bool { true }

    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 126, 124: step(by: 1)      // up, right
        case 125, 123: step(by: -1)     // down, left
        case 36, 76: start()            // return, enter
        default: super.keyDown(with: event)
        }
    }

    override func accessibilityPerformIncrement() -> Bool {
        step(by: 1)
        return true
    }

    override func accessibilityPerformDecrement() -> Bool {
        step(by: -1)
        return true
    }

    /// The disc grown into place out of the status item above it, the way the
    /// recording island arrives, and for the same reason: it says where the
    /// window came from.
    func appear() {
        guard !reduceMotion, let layer else { return }
        let growing = CABasicAnimation(keyPath: "transform")
        growing.fromValue = NSValue(caTransform3D: RecordingPanelView.scaled(0.92, about: layer.bounds))
        growing.toValue = NSValue(caTransform3D: CATransform3DIdentity)
        growing.duration = RecordingPanelView.appearing
        growing.timingFunction = RecordingPanelView.easing
        layer.add(growing, forKey: "appear")
    }
}

/// The middle of the dial: what it is set to, and the one thing to do about
/// it. It is a button because that is what the middle of this dial is for —
/// turn the ring, press the middle, the tape rolls — and the readout lives on
/// it so there is nothing on the disc that is not either turned or pressed.
final class DialCentreButton: NSButton {
    private static let face = NSColor.white.withAlphaComponent(0.06)
    private static let pressed = NSColor.white.withAlphaComponent(0.12)
    private static let rim = NSColor.white.withAlphaComponent(0.1)

    private var number = ""
    private var unit = ""

    init() {
        super.init(frame: .zero)
        isBordered = false
        setButtonType(.momentaryChange)
        title = ""
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("DialCentreButton is built in code, not loaded from a nib")
    }

    /// Minutes up to the hour, hours and minutes past it — "48 MIN", "1:30 HR" —
    /// which is how a meeting's length is said aloud.
    func show(minutes: Int) {
        if minutes < 60 {
            number = "\(minutes)"
            unit = "MIN"
        } else {
            number = String(format: "%d:%02d", minutes / 60, minutes % 60)
            unit = "HR"
        }
        setAccessibilityLabel("Start recording for \(minutes) minutes")
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let face = NSBezierPath(ovalIn: bounds.insetBy(dx: 0.5, dy: 0.5))
        (isHighlighted ? DialCentreButton.pressed : DialCentreButton.face).setFill()
        face.fill()
        DialCentreButton.rim.setStroke()
        face.lineWidth = 1
        face.stroke()

        let centred = NSMutableParagraphStyle()
        centred.alignment = .center
        let big = NSAttributedString(string: number, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 30, weight: .semibold),
            .foregroundColor: NSColor.white,
            .paragraphStyle: centred,
        ])
        let small = NSAttributedString(string: unit, attributes: [
            .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
            .foregroundColor: NSColor.white.withAlphaComponent(0.55),
            .kern: 1.5,
            .paragraphStyle: centred,
        ])
        let bigHeight = big.size().height
        let smallHeight = small.size().height
        let gap: CGFloat = 1
        let top = bounds.midY + (bigHeight + gap + smallHeight) / 2
        big.draw(in: NSRect(x: 0, y: top - bigHeight, width: bounds.width, height: bigHeight))
        small.draw(in: NSRect(
            x: 0, y: top - bigHeight - gap - smallHeight, width: bounds.width, height: smallHeight
        ))
    }
}

// MARK: - where the dial lives

/// A window that may hang off the edge of the screen. AppKit pulls a window
/// back on screen whenever it is placed or resized — that is what a tucked dial
/// must not have happen to it — and this is the one override that stops it.
final class DialPanel: NSPanel {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        frameRect
    }
}

/// Where the dial was left, as a thing that can be written down and read back.
/// A free dial is a fraction of its screen rather than a point, so a display
/// that changes resolution keeps it in the same place; a tucked one is an edge
/// and how far along it. The screen is named by its display number, and a
/// display that is no longer attached puts the dial back under the menu bar.
enum DialPlace: Equatable {
    case hanging
    case free(display: UInt32, x: CGFloat, y: CGFloat)
    case tucked(DialDock.Edge, display: UInt32, along: CGFloat)

    var encoded: String {
        switch self {
        case .hanging:
            return "hanging"
        case let .free(display, x, y):
            return "free|\(display)|\(x)|\(y)"
        case let .tucked(edge, display, along):
            return "tucked|\(edge.rawValue)|\(display)|\(along)"
        }
    }

    init?(encoded: String) {
        let parts = encoded.split(separator: "|").map(String.init)
        switch parts.first {
        case "hanging":
            self = .hanging
        case "free":
            guard parts.count == 4, let display = UInt32(parts[1]),
                  let x = Double(parts[2]), let y = Double(parts[3]) else { return nil }
            self = .free(display: display, x: x, y: y)
        case "tucked":
            guard parts.count == 4, let edge = DialDock.Edge(rawValue: parts[1]),
                  let display = UInt32(parts[2]), let along = Double(parts[3]) else { return nil }
            self = .tucked(edge, display: display, along: along)
        default:
            return nil
        }
    }
}

/// The dial's window and the four places it can be: hanging under the status
/// item like a menu, free anywhere on the screen, tucked into an edge with a
/// sliver showing, or peeking back out of that edge for as long as the pointer
/// is on it. The view inside knows nothing of this — it reports a grip on its
/// ground and the pointer crossing its edge, and this decides what those mean.
///
/// Moving is done by hand, event by event, rather than handed to the window
/// server: the release is what decides whether the dial tucks, and a release
/// this object never hears about is a decision it cannot make.
///
/// Tucked, it costs nothing: no timer, no monitor, one tracking area waiting
/// for a crossing. The dwell and the grace are timers that exist only between
/// a crossing and its re-check — and both re-check, because a pointer that
/// skimmed the sliver on its way somewhere else is not a pointer asking for
/// the dial.
final class DialDock {
    enum Edge: String {
        case left, right, top, bottom
    }

    enum State {
        case hanging
        case free
        case tucked(Edge)
        case peeking(Edge)

        var isHanging: Bool {
            if case .hanging = self { return true }
            return false
        }
    }

    /// How much of the disc stays on screen when tucked: the rim and the first
    /// few ticks, enough to be a tab and to be hovered. Never less than a few
    /// points — a window with nothing on screen gets no events at all.
    static let peek: CGFloat = 22

    /// How near the pointer must be let go to an edge for the dial to tuck.
    static let snap: CGFloat = 8

    /// The dwell before a hover reveals, and the grace after the pointer leaves
    /// before it hides again — each re-checked when it fires. A fifth of a
    /// second is what the Dock waits; the grace is longer because coming back
    /// for something is more common than leaving for good.
    static let dwell: TimeInterval = 0.2
    static let grace: TimeInterval = 0.4

    /// The slide, the same length from every edge: a window that took longer
    /// from farther away would be a window that felt heavier at the bottom of
    /// the screen than at the side.
    static let sliding: TimeInterval = 0.22
    static let tuckedAlpha: CGFloat = 0.6

    /// The air between a revealed disc and the edge it came out of.
    static let clearance: CGFloat = 6

    let panel: NSPanel
    let view: DialView
    private let reduceMotion: Bool
    private(set) var state: State = .hanging

    /// How far along its edge a tucked dial sits, as a fraction, kept so that
    /// peeking out and tucking back land on the same spot.
    private var along: CGFloat = 0.5

    /// The pointer's offset from the window's origin while the dial is being
    /// moved, and nothing at any other time.
    private var grab: NSPoint?
    private var pending: Timer?
    private var screensChanged: Any?

    /// Told once, when the dial stops hanging: whoever hung it there closes it
    /// on a click elsewhere and lights the button it hangs from, and a dial that
    /// has been carried off is neither of those things any more.
    var onLeftHanging: (() -> Void)?

    /// Told whenever where the dial lives changes, with the place to remember.
    var onPlaced: ((DialPlace) -> Void)?

    init(panel: NSPanel, view: DialView, reduceMotion: Bool) {
        self.panel = panel
        self.view = view
        self.reduceMotion = reduceMotion
        view.onGrip = { [weak self] grip in self?.gripped(grip) }
        view.onHover = { [weak self] inside in self?.hovered(inside) }
        // a display arriving or leaving moves every edge; a tucked dial is put
        // back against the edge it was tucked into, on whatever is there now
        screensChanged = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil,
            queue: .main
        ) { [weak self] _ in self?.reanchor() }
    }

    deinit {
        pending?.invalidate()
        if let screensChanged {
            NotificationCenter.default.removeObserver(screensChanged)
        }
    }

    // --- putting it back where it was ---------------------------------------------

    /// The dial put back where it was left, or nothing — and then the caller
    /// hangs it under the status item — when that place is gone.
    func restore(_ place: DialPlace) -> Bool {
        switch place {
        case .hanging:
            return false
        case let .free(display, x, y):
            guard let screen = DialDock.screen(numbered: display) else { return false }
            let area = screen.visibleFrame
            let origin = NSPoint(
                x: area.minX + x * area.width - panel.frame.width / 2,
                y: area.minY + y * area.height - panel.frame.height / 2
            )
            panel.setFrameOrigin(DialDock.clamped(origin, size: panel.frame.size, within: area))
            state = .free
            panel.alphaValue = 1
            panel.orderFrontRegardless()
            return true
        case let .tucked(edge, display, along):
            guard let screen = DialDock.screen(numbered: display) else { return false }
            self.along = along
            state = .tucked(edge)
            panel.setFrame(tuckedFrame(edge, on: screen), display: false)
            panel.alphaValue = DialDock.tuckedAlpha
            panel.orderFrontRegardless()
            return true
        }
    }

    /// The dial brought to where it can be used: out of its edge, or to the
    /// front. This is the way in that needs no pointer.
    func reveal() {
        pending?.invalidate()
        if case let .tucked(edge) = state {
            peek(edge)
        }
        panel.orderFrontRegardless()
    }

    private var place: DialPlace {
        let screen = DialDock.screen(under: panel.frame) ?? NSScreen.main
        guard let screen, let display = DialDock.number(of: screen) else { return .hanging }
        switch state {
        case .hanging:
            return .hanging
        case .free:
            let area = screen.visibleFrame
            return .free(
                display: display,
                x: (panel.frame.midX - area.minX) / area.width,
                y: (panel.frame.midY - area.minY) / area.height
            )
        case let .tucked(edge), let .peeking(edge):
            return .tucked(edge, display: display, along: along)
        }
    }

    // --- being moved ------------------------------------------------------------

    private func gripped(_ grip: DialView.Grip) {
        switch grip {
        case let .began(point):
            pending?.invalidate()
            grab = NSPoint(x: point.x - panel.frame.minX, y: point.y - panel.frame.minY)
            NSCursor.closedHand.push()
        case let .moved(point):
            guard let grab else { return }
            leaveWherever()
            panel.setFrameOrigin(NSPoint(x: point.x - grab.x, y: point.y - grab.y))
        case let .ended(point):
            NSCursor.pop()
            guard grab != nil else { return }
            grab = nil
            release(at: point)
        }
    }

    /// The first movement of a grip: a hanging dial stops hanging, a tucked or
    /// peeking one is just a window again, and either way it is fully lit.
    private func leaveWherever() {
        switch state {
        case .hanging:
            state = .free
            onLeftHanging?()
        case .tucked, .peeking:
            state = .free
        case .free:
            break
        }
        panel.alphaValue = 1
    }

    /// Let go: within a few points of an edge it tucks into that edge, at the
    /// spot along it where it was dropped; anywhere else it stays.
    private func release(at point: NSPoint) {
        guard let screen = DialDock.screen(containing: point) else {
            state = .free
            onPlaced?(place)
            return
        }
        if let edge = DialDock.edge(near: point, of: screen) {
            along = DialDock.along(edge, of: panel.frame, on: screen)
            tuck(edge, on: screen)
        } else {
            state = .free
            onPlaced?(place)
        }
    }

    // --- being hovered --------------------------------------------------------------

    /// The pointer crossing the dial's edge, in or out. A crossing sets a timer
    /// and the timer asks again: what reveals a tucked dial is a pointer still
    /// on it a fifth of a second later, and what hides a peeking one is a
    /// pointer still gone after the grace — and never one that is holding it.
    private func hovered(_ inside: Bool) {
        pending?.invalidate()
        pending = nil
        switch (state, inside) {
        case let (.tucked(edge), true):
            wait(DialDock.dwell) { [weak self] in
                guard let self, self.pointerIsOnTheDial else { return }
                self.peek(edge)
            }
        case let (.peeking(edge), false):
            wait(DialDock.grace) { [weak self] in
                guard let self, self.grab == nil, !self.pointerIsOnTheDial else { return }
                guard let screen = DialDock.screen(under: self.panel.frame) else { return }
                self.tuck(edge, on: screen)
            }
        default:
            break
        }
    }

    private func wait(_ seconds: TimeInterval, then act: @escaping () -> Void) {
        let timer = Timer(timeInterval: seconds, repeats: false) { [weak self] _ in
            self?.pending = nil
            act()
        }
        timer.tolerance = seconds / 10
        RunLoop.main.add(timer, forMode: .common)
        pending = timer
    }

    private var pointerIsOnTheDial: Bool {
        panel.frame.contains(NSEvent.mouseLocation)
    }

    // --- the two positions at an edge ---------------------------------------------------

    private func tuck(_ edge: Edge, on screen: NSScreen) {
        state = .tucked(edge)
        slide(to: tuckedFrame(edge, on: screen), alpha: DialDock.tuckedAlpha)
        onPlaced?(place)
    }

    private func peek(_ edge: Edge) {
        guard let screen = DialDock.screen(under: panel.frame) ?? NSScreen.main else { return }
        state = .peeking(edge)
        slide(to: revealedFrame(edge, on: screen), alpha: 1)
    }

    /// A tucked or peeking dial put back against its edge after the screens
    /// changed under it — on the screen it is now nearest, which may be a
    /// different one from the one it was tucked into.
    private func reanchor() {
        guard let screen = DialDock.screen(under: panel.frame) ?? NSScreen.main else { return }
        switch state {
        case let .tucked(edge):
            panel.setFrame(tuckedFrame(edge, on: screen), display: true)
        case let .peeking(edge):
            panel.setFrame(revealedFrame(edge, on: screen), display: true)
        case .free:
            panel.setFrameOrigin(DialDock.clamped(
                panel.frame.origin, size: panel.frame.size, within: screen.visibleFrame
            ))
        case .hanging:
            break
        }
    }

    /// The window moved and dimmed in one fixed-length ease-out, or at once
    /// under Reduce Motion: a disc sliding two hundred points is the large
    /// movement that setting asks not to see.
    private func slide(to frame: NSRect, alpha: CGFloat) {
        guard !reduceMotion else {
            panel.setFrame(frame, display: true)
            panel.alphaValue = alpha
            return
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = DialDock.sliding
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().setFrame(frame, display: true)
            panel.animator().alphaValue = alpha
        }
    }

    /// The whole disc just inside the edge, at `along` of the way across it.
    private func revealedFrame(_ edge: Edge, on screen: NSScreen) -> NSRect {
        let area = screen.visibleFrame
        let size = panel.frame.size
        let disc = DialView.disc
        let margin = DialView.margin
        let gap = DialDock.clearance
        var origin = NSPoint.zero
        switch edge {
        case .left:
            origin.x = area.minX + gap - margin
            origin.y = area.minY + along * area.height - size.height / 2
        case .right:
            origin.x = area.maxX - gap - disc - margin
            origin.y = area.minY + along * area.height - size.height / 2
        case .top:
            origin.y = area.maxY - gap - disc - margin
            origin.x = area.minX + along * area.width - size.width / 2
        case .bottom:
            origin.y = area.minY + gap - margin
            origin.x = area.minX + along * area.width - size.width / 2
        }
        return NSRect(origin: DialDock.clamped(origin, size: size, within: area), size: size)
    }

    /// The revealed frame pushed out through its edge until `peek` points of
    /// the disc are left. The window keeps its size; only where it is moves.
    private func tuckedFrame(_ edge: Edge, on screen: NSScreen) -> NSRect {
        var frame = revealedFrame(edge, on: screen)
        let out = DialView.disc - DialDock.peek + DialDock.clearance
        switch edge {
        case .left: frame.origin.x -= out
        case .right: frame.origin.x += out
        case .top: frame.origin.y += out
        case .bottom: frame.origin.y -= out
        }
        return frame
    }

    // --- screens ---------------------------------------------------------------------

    /// The nearest edge within reach of a point, or nothing. Tested against the
    /// screen's whole frame rather than the part the menu bar and Dock leave,
    /// because a dial dropped under either of them was dropped at the edge.
    static func edge(near point: NSPoint, of screen: NSScreen) -> Edge? {
        let frame = screen.frame
        let distances: [(Edge, CGFloat)] = [
            (.left, point.x - frame.minX), (.right, frame.maxX - point.x),
            (.top, frame.maxY - point.y), (.bottom, point.y - frame.minY),
        ]
        return distances.filter { $0.1 <= snap }.min { $0.1 < $1.1 }?.0
    }

    /// How far along an edge a frame's middle sits, as a fraction of the screen.
    static func along(_ edge: Edge, of frame: NSRect, on screen: NSScreen) -> CGFloat {
        let area = screen.visibleFrame
        switch edge {
        case .left, .right:
            return min(max((frame.midY - area.minY) / area.height, 0), 1)
        case .top, .bottom:
            return min(max((frame.midX - area.minX) / area.width, 0), 1)
        }
    }

    /// The screen a point is on, with its far edges counted in: a rectangle's
    /// `contains` leaves out its own right and top, which is exactly where a
    /// pointer pushed against an edge sits.
    static func screen(containing point: NSPoint) -> NSScreen? {
        NSScreen.screens.first { $0.frame.insetBy(dx: -1, dy: -1).contains(point) }
    }

    /// The screen a frame is mostly on, by its middle first and any overlap
    /// second — a tucked window's middle is off every screen.
    static func screen(under frame: NSRect) -> NSScreen? {
        screen(containing: NSPoint(x: frame.midX, y: frame.midY))
            ?? NSScreen.screens.max { $0.frame.intersection(frame).area < $1.frame.intersection(frame).area }
    }

    static func number(of screen: NSScreen) -> UInt32? {
        screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32
    }

    static func screen(numbered display: UInt32) -> NSScreen? {
        NSScreen.screens.first { number(of: $0) == display }
    }

    static func clamped(_ origin: NSPoint, size: NSSize, within area: NSRect) -> NSPoint {
        NSPoint(
            x: min(max(origin.x, area.minX), max(area.minX, area.maxX - size.width)),
            y: min(max(origin.y, area.minY), max(area.minY, area.maxY - size.height))
        )
    }
}

extension NSRect {
    var area: CGFloat { isNull ? 0 : width * height }
}

// MARK: - end of meters and marks

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// accessory: this app is its menu bar item and nothing else — no Dock tile, no
// window, and no place in the app switcher for something with nothing to show
app.setActivationPolicy(.accessory)
app.run()
