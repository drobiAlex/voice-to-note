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

    /// The pulse belongs to one state and must not outlive it: a timer left
    /// running would go on redrawing a button that has moved on to saying
    /// something else entirely. Hanging it off the state itself is what makes
    /// every way out of processing — finished, failed, quit — stop it too.
    private var state = RecorderState.idle {
        didSet {
            if state == .processing {
                startPulsing()
            } else {
                stopPulsing()
            }
            // a rolling tape is the one state the status item answers a click
            // with a window instead of a menu, and the meters belong to it for
            // the same reason the pulse does. Emptying them on every state that
            // is not recording is also what makes each recording start from
            // silence rather than from whatever the last one ended on
            if state == .recording {
                openToThePanel()
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
    private var pulse: Timer?
    private var pulseFrame = 0
    private var mayNotify = false
    private var logFile: FileHandle?
    private var projects: [(name: String, count: Int)]?
    private var devices: AudioDevices?
    private var refreshing = false

    /// How loud each side has been, kept for the length of one recording. The
    /// two of them are the model behind everything the meters draw, and the
    /// state's own didSet is what empties them — a strip still showing the last
    /// meeting's voices would be describing a tape that stopped.
    private let systemLevels = LevelHistory()
    private let microphoneLevels = LevelHistory()

    /// The floating panel a recording is watched in, and the view inside it.
    /// The two live and die together: the view copies whatever it draws out of
    /// the histories every time it is told to, so there is nothing in it worth
    /// keeping once it is off screen, and building a fresh one on every opening
    /// is also what asks about Reduce Motion again rather than once at launch.
    private var panel: NSPanel?
    private var panelView: RecordingPanelView?

    /// How long a panel that opened itself stays up: long enough to read the
    /// header and watch both meters move, and over before it is in the way of
    /// anything. What it is for is a moment's confirmation that the tape is
    /// rolling, not a window somebody has been handed to get rid of.
    private static let glance: TimeInterval = 4

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

    /// The composite handed to the button last, and when. Only Reduce Motion
    /// looks at them: it asks for the strips to be redrawn twice a second
    /// instead of ten times, and the way to honour that is to hand back what
    /// was drawn before rather than to stop the readings arriving.
    private var drawnMeters: NSImage?
    private var drawnAt = Date.distantPast

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
        askToNotify()
        refresh()
    }

    /// A recording in progress is left running rather than waited on: it is its
    /// own process, and the minutes of transcription after the tape stops are
    /// no reason to hold a quit. Stopping the tape first is what makes the
    /// meeting up to this point a memo instead of two half-written files.
    func applicationWillTerminate(_ notification: Notification) {
        stopPulsing()
        if let recorder, recorder.isRunning, state == .starting || state == .recording {
            recorder.interrupt()
        }
    }

    // --- the button in the menu bar -----------------------------------------

    /// Draws the state the recording is actually in. The elapsed time rides on
    /// the button rather than only in the menu, since the whole point of the
    /// thing is being able to see at a glance that a meeting is being taped.
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
            button.image = meters()
            button.attributedTitle = label(elapsed())
        case .processing:
            button.image = Mark.working[pulseFrame]
            button.attributedTitle = NSAttributedString(string: "")
        }
    }

    /// The mark with both strips beside it, as the two sides stand right now.
    /// Under Reduce Motion the last one is handed back until half a second has
    /// gone by: that setting asks for no scrolling history, and columns
    /// redrawn ten times a second are the very movement at the edge of the eye
    /// it exists to spare somebody.
    private func meters() -> NSImage {
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let now = Date()
        if reduceMotion, let drawn = drawnMeters, now.timeIntervalSince(drawnAt) < 0.5 {
            return drawn
        }
        let image = Meters.composite(
            mark: Mark.recording, system: systemLevels, microphone: microphoneLevels,
            reduceMotion: reduceMotion
        )
        // the sentence is composed once a second, but every composite has to
        // carry it: the image is replaced ten times a second and a screen
        // reader describes whichever one it finds on the button
        image.accessibilityDescription = spoken
        drawnMeters = image
        drawnAt = now
        return image
    }

    /// A fresh reading from each side. These are the only clock the strips
    /// have — they arrive ten a second for as long as the tape rolls — which
    /// is why nothing here starts a timer, and why the elapsed time, the
    /// spoken sentence and the menu's footer are left to the one that exists.
    private func metered(_ system: Double, _ microphone: Double) {
        systemLevels.push(system)
        microphoneLevels.push(microphone)
        panelView?.show(system: systemLevels, microphone: microphoneLevels)
        statusItem.button?.image = meters()
    }

    /// The recording put into a sentence, for a screen reader and for the
    /// pointer resting on the button: how far in it is and what each side is
    /// doing. Once a second, from the ticker, because formatting this ten
    /// times a second would buy nothing that can be read in a tenth of one.
    private func speak() {
        let now = Date()
        spoken = "Recording \(elapsed()) — "
            + Meters.spoken("system audio", systemLevels, now: now) + ", "
            + Meters.spoken("microphone", microphoneLevels, now: now)
        statusItem.button?.toolTip = spoken
        panelView?.showElapsed(elapsed())
        panelView?.showFooter(system: systemLevels, microphone: microphoneLevels, now: now)
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
        drawnMeters = nil
        spoken = ""
        statusItem.button?.toolTip = nil
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
        // the common modes, or the clock would freeze for as long as a menu is
        // held open — which is exactly when somebody is looking at it
        RunLoop.main.add(ticker, forMode: .common)
        self.ticker = ticker
    }

    private func stopTicking() {
        ticker?.invalidate()
        ticker = nil
    }

    /// The two frames of the mark, swapped slowly. Transcribing a meeting takes
    /// minutes with nothing else on screen to show for it, and this is the only
    /// thing saying the work is still going rather than quietly dead. Slow on
    /// purpose: a beat under a second is movement caught out of the corner of
    /// an eye, and anything quicker is a thing demanding to be looked at.
    private func startPulsing() {
        stopPulsing()
        let pulse = Timer(timeInterval: 0.9, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.pulseFrame = (self.pulseFrame + 1) % Mark.working.count
            self.show()
        }
        // the common modes, for the same reason the clock is added to them: a
        // menu held open must not be the thing that freezes the animation
        RunLoop.main.add(pulse, forMode: .common)
        self.pulse = pulse
    }

    /// Back to the first frame as well as stopped, so the next spell of
    /// processing starts from the mark at rest instead of wherever the last one
    /// happened to be interrupted.
    private func stopPulsing() {
        pulse?.invalidate()
        pulse = nil
        pulseFrame = 0
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
        switch state {
        case .idle:
            menu.addItem(action("Start Recording", #selector(startRecording), key: "r"))
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
            menu.addItem(note(state == .recording ? "Recording \(elapsed())" : "Starting …"))
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
        view.onStop(self, #selector(stopRecording))
        // a pointer arriving inside the panel is somebody having taken it up on
        // what it is showing, and from that moment the window is theirs to
        // close: one that vanished out from under a pointer resting on it would
        // be this app deciding it had been looked at for long enough
        view.onEngage = { [weak self] in self?.stopGlancing() }
        view.show(system: systemLevels, microphone: microphoneLevels)
        view.showElapsed(elapsed())
        view.showFooter(system: systemLevels, microphone: microphoneLevels)
        let panel = NSPanel(
            contentRect: view.frame, styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.contentView = view
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.panel = panel
        self.panelView = view
        place(panel)
        arrive(panel, view, reduceMotion: reduceMotion)
        // the button stays lit for as long as the panel is up, which is the only
        // thing tying the window under the menu bar to the item it came out of
        statusItem.button?.highlight(true)
        watchForClicks()
    }

    /// The panel put on screen as the status item growing downwards rather than
    /// as a window switched on: the whole thing fades up while the view inside
    /// it grows the last few per cent into place, on one curve so the two read
    /// as a single movement. The window's frame never moves — it sits at the
    /// menu bar's own level, and a panel that slid down into place would be
    /// drawn over the menu bar for as long as it took to arrive.
    ///
    /// Reduce Motion gets the panel and none of this. The opening itself still
    /// happens either way: a window saying the tape is rolling is information,
    /// and it is only the way it arrives that is decoration.
    private func arrive(_ panel: NSPanel, _ view: RecordingPanelView, reduceMotion: Bool) {
        guard !reduceMotion else {
            panel.orderFrontRegardless()
            return
        }
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        view.appear()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = RecordingPanelView.appearing
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
        if let clicksElsewhere {
            NSEvent.removeMonitor(clicksElsewhere)
        }
        if let clicksHere {
            NSEvent.removeMonitor(clicksHere)
        }
        clicksElsewhere = nil
        clicksHere = nil
        statusItem.button?.highlight(false)
        let closing = panel
        panel = nil
        panelView = nil
        vanish(closing)
    }

    /// The window taken off screen, faded first wherever anything is allowed to
    /// move. It goes in half the time it came: arriving is the part somebody
    /// watches, and a window that takes as long to leave is a window in the way
    /// of whatever the click that dismissed it was meant for.
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
        // the common modes, for the same reason the clock and the pulse are put
        // in them: a menu held open somewhere else must not be the thing that
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

    /// Flush under the menu bar and centred on the button it belongs to, so the
    /// panel reads as the status item having grown downwards rather than as a
    /// window that happens to be near it. That is what the flat top edge is for,
    /// and it only works if the top edge is exactly the menu bar's bottom.
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
        panel.setFrameOrigin(NSPoint(x: x.rounded(), y: (anchor.minY - size.height).rounded()))
    }

    /// What closes the panel: a click anywhere that is not the panel itself.
    /// The global monitor sees the clicks that go to other apps, the local one
    /// sees the clicks that come here — and the local one has to let the status
    /// item's own window through, or clicking the button a second time would
    /// close the panel a moment before the button's action reopened it.
    private func watchForClicks() {
        let elsewhere: NSEvent.EventTypeMask = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        clicksElsewhere = NSEvent.addGlobalMonitorForEvents(matching: elsewhere) { [weak self] _ in
            self?.closePanel()
        }
        clicksHere = NSEvent.addLocalMonitorForEvents(matching: elsewhere) { [weak self] event in
            guard let self else { return event }
            let ours = [self.panel, self.statusItem.button?.window]
            if !ours.contains(where: { $0 === event.window }) {
                self.closePanel()
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

    @objc private func startRecording() {
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
    /// the numbers it prints instead are what the strips in the menu bar are.
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
        guard state == .starting || state == .recording else { return }
        guard let recorder, recorder.isRunning else { return }
        // the same interrupt a Ctrl+C in a terminal sends: the recorder closes
        // the two tracks and then spends minutes merging and transcribing them,
        // which is why stopping the tape is not the end of the session
        recorder.interrupt()
        stopTicking()
        state = .processing
        show()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    /// Everything the recorder says on its way through a meeting: the meter's
    /// readings, which feed the strips and go no further, and every other line,
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
/// only its colour, and a slow shuffle while notes are being made. What is
/// in the corner of the screen stays the same thing all the way through a
/// meeting; what it is doing is told by how it is inked.
///
/// Each image is built once and kept. A status item redraws its button on
/// every appearance change and once a second while a recording is running,
/// and none of that should cost a fresh bitmap.
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

    /// The same five bars with the flanks traded and the tall one held
    /// where it is. Two frames of this is the whole processing animation:
    /// the mark leans rather than spins or blinks, because something
    /// flashing in the menu bar reads as a thing gone wrong rather than a
    /// thing under way. Pinning the tall bar is what keeps the two frames
    /// one mark swaying instead of two different marks alternating.
    private static let leaning: [CGFloat] = [6, 10, 14, 8, 4]

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

    static let working = [
        draw(resting, .black, template: true, "Making notes from the meeting"),
        draw(leaning, .black, template: true, "Making notes from the meeting"),
    ]

    /// One frame of the mark: bars of the given heights, rounded at both
    /// ends, laid out from a left edge that centres the whole run inside
    /// the square, so every frame sits in exactly the same place and the
    /// animation is the bars moving rather than the mark shifting sideways.
    private static func draw(
        _ heights: [CGFloat], _ ink: NSColor, template: Bool, _ description: String
    ) -> NSImage {
        let run = CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * gap
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            ink.set()
            var x = (side - run) / 2
            for height in heights {
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
}

/// How loud one side of a recording has been over the last couple of seconds.
/// Two of these — the system tap and the microphone — are the whole model both
/// meters are drawn from: the strip in the menu bar draws the ring, the level
/// indicator in the menu draws `latest`, and the silence clock is what lets
/// either of them say a microphone has been dead for twelve seconds rather
/// than only that it happens to be quiet at this instant.
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
    /// behind it is what a scrolling strip wants.
    private(set) var latest = LevelHistory.floor

    /// When this side went quiet, or nothing while it is not. Kept as the
    /// moment rather than a running count so nothing has to tick to age it:
    /// whoever asks does the subtraction.
    private(set) var silentSince: Date?

    private var ring: [Double]

    /// The slot the next reading overwrites, which is also the oldest one.
    private var next = 0

    /// Filled with the floor rather than left short, so a strip has a whole
    /// row to draw from its first frame. Before any reading has arrived there
    /// genuinely is no sound, and drawing that as silence is honest; a row
    /// that grew in from the right would instead be claiming the meeting is
    /// younger than it is every time this is emptied.
    ///
    /// The ring is as long as the widest thing drawn from it, which is the
    /// panel's waveform. The strip beside the mark wants a fraction of that
    /// and takes the newest end of it — one history for both is what keeps the
    /// two from ever disagreeing about what the last second sounded like.
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
    /// ring — and a strip has to be one moment rather than a smear of two.
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

/// The strips that ride beside the mark while the tape rolls, and the words
/// both meters are described in. Drawing and phrasing only: handed the two
/// histories it gives back an image, which is what lets the whole look of the
/// thing be rendered to a file and judged without a meeting being recorded.
enum Meters {
    /// One column per reading and a reading every 100 ms, so eighteen columns
    /// is 1.8 seconds of history — about as much as is taken in at a glance,
    /// and as much as 36 points holds at a width where a bar is still a bar.
    static let columns = 18

    /// Silence is worth mentioning after ten seconds and not before. Pauses
    /// are ordinary in a meeting — somebody reading a slide, a question being
    /// thought about — and a warning that fires on all of them is a warning
    /// that gets ignored on the one that means a muted microphone.
    static let silenceAfter: TimeInterval = 10

    /// The mark's square, the gap that keeps the strips from reading as part
    /// of it, and the block they are drawn in.
    private static let side: CGFloat = 18
    private static let gap: CGFloat = 6
    private static let stripWidth: CGFloat = 36

    /// Two rows of eight points with two between them fills the same 18pt the
    /// mark is tall, which is what keeps the whole thing one status item high.
    private static let rowHeight: CGFloat = 8
    private static let rowGap: CGFloat = 2

    /// A point of bar and a point of air. Any wider and 1.8 seconds does not
    /// fit; any narrower and there is no bar left to see.
    private static let columnWidth: CGFloat = 2
    private static let barWidth: CGFloat = 1

    /// The height of the track a level fills under Reduce Motion. Half the
    /// row, so an empty one is plainly a container waiting to be filled rather
    /// than a line that might be a flat signal.
    private static let trackHeight: CGFloat = 4

    /// Even numbers only, and the same reason the mark's are: a bar is
    /// mirrored around the middle of its row, so an odd height puts both of
    /// its ends on a half point, and a half-point edge is a grey smear rather
    /// than a line on a display that is not retina.
    private static let heights: [CGFloat] = [2, 4, 6, 8]

    /// Fixed, whatever the levels do. Everything to the right of a menu bar
    /// extra shifts when it changes width, so a status item that grew and
    /// shrank with the loudness of the room would keep the entire menu bar
    /// twitching for the length of a meeting.
    static var size: NSSize {
        NSSize(width: side + gap + stripWidth, height: side)
    }

    /// The red mark with both sides of the recording beside it: system audio
    /// on top, microphone below, the way they are named everywhere else.
    ///
    /// The two histories are read out here and the closure keeps the copies,
    /// not the histories, because the closure runs when the button draws —
    /// which is after the next reading has already arrived.
    ///
    /// Only the newest end of each history is taken. The ring behind it is as
    /// long as the panel's waveform needs, and a strip 36 points wide asked to
    /// draw all of it would either shrink every column below a point or throw
    /// away every other reading — either way a menu bar that changed its look
    /// the day a panel was added to the app.
    static func composite(
        mark: NSImage, system: LevelHistory, microphone: LevelHistory, reduceMotion: Bool
    ) -> NSImage {
        let top = Array(system.trace.suffix(columns))
        let bottom = Array(microphone.trace.suffix(columns))
        let image = NSImage(size: size, flipped: false) { _ in
            mark.draw(in: NSRect(x: 0, y: 0, width: side, height: side))
            row(top, from: side + gap, midline: rowHeight + rowGap + rowHeight / 2,
                reduceMotion: reduceMotion)
            row(bottom, from: side + gap, midline: rowHeight / 2, reduceMotion: reduceMotion)
            return true
        }
        // not a template image: the mark's red is the one colour in this app
        // that means something, and a template is flattened to the menu bar's
        // own tint. The strips ask for a label colour instead, which comes to
        // the same thing for the half that should follow the menu bar
        image.isTemplate = false
        return image
    }

    /// One side's history, oldest at the left and the newest reading against
    /// the right edge — the direction a waveform has been read since tape.
    private static func row(
        _ trace: [Double], from left: CGFloat, midline: CGFloat, reduceMotion: Bool
    ) {
        guard let newest = trace.last else { return }
        if reduceMotion {
            fill(newest, from: left, midline: midline)
            return
        }
        for (column, level) in trace.enumerated() {
            // the newest column is nearly solid and the oldest is a ghost, so
            // which end of the strip is now needs no explaining
            let age = CGFloat(column) / CGFloat(max(trace.count - 1, 1))
            bar(level, from: left + CGFloat(column) * columnWidth, width: barWidth,
                flat: columnWidth, midline: midline, ink: 0.3 + 0.6 * age)
        }
    }

    /// The newest reading as a track filling from the left, which is what
    /// Reduce Motion leaves room for: nothing scrolls past, no column flickers
    /// ten times a second, and the one thing the strip exists to show — how
    /// loud this side is — is still there to be read. The scale is the whole
    /// of it, floor to ceiling, because a fill has no shape to say anything
    /// with and the length is all there is.
    ///
    /// Silence empties the track rather than shortening it, and the track is
    /// drawn either way: an empty container is what says nothing is arriving,
    /// where a missing one would only look like a strip that failed to draw.
    private static func fill(_ level: Double, from left: CGFloat, midline: CGFloat) {
        let track = NSRect(
            x: left, y: midline - trackHeight / 2, width: stripWidth, height: trackHeight
        )
        NSColor.labelColor.withAlphaComponent(0.2).setFill()
        track.fill()
        guard level >= LevelHistory.silence else { return }
        let span = LevelHistory.ceiling - LevelHistory.floor
        let loud = (min(level, LevelHistory.ceiling) - LevelHistory.floor) / span
        NSColor.labelColor.withAlphaComponent(0.9).setFill()
        // rounded to a whole point, for the same reason every other edge here
        // is: a fill ending on a half point frays instead of stopping
        NSRect(
            x: track.minX, y: track.minY, width: (CGFloat(loud) * stripWidth).rounded(),
            height: trackHeight
        ).fill()
    }

    /// One reading, mirrored around the middle of its row the way a waveform
    /// is. Silence is not the shortest bar but a flat line: a difference in
    /// shape, which somebody reads at a glance and still reads with the colour
    /// taken away, where a two-point bar beside a four-point one is a guess.
    ///
    /// A flat line is drawn the full width of its column rather than the
    /// width of a bar, so that neighbouring silent readings meet and make one
    /// line: a row of dots at the spacing of the bars is a texture, and the
    /// shape somebody has to recognise here is a flatline.
    ///
    /// The ink is resolved in here, inside the drawing handler, instead of
    /// being passed in: this runs when the button draws, and only then is the
    /// appearance the menu bar is actually wearing — light, dark, or vibrancy
    /// over a desktop picture — the current one.
    private static func bar(
        _ level: Double, from left: CGFloat, width: CGFloat, flat: CGFloat, midline: CGFloat,
        ink: CGFloat
    ) {
        guard level >= LevelHistory.silence else {
            // dimmer again than a quiet bar, and sitting a whole point under
            // the midline rather than astride it, since a one-point line
            // centred there would land half in each of two rows of pixels
            NSColor.labelColor.withAlphaComponent(ink * 0.6).setFill()
            NSRect(x: left, y: midline - 1, width: flat, height: 1).fill()
            return
        }
        let height = barHeight(for: level)
        NSColor.labelColor.withAlphaComponent(ink).setFill()
        NSRect(x: left, y: midline - height / 2, width: width, height: height).fill()
    }

    /// Which of the four heights a reading lands on. The scale runs −50 dBFS
    /// to 0, everything below being silence with a shape of its own, and four
    /// steps is as much as eight points of row can tell apart — a fifth would
    /// be a difference nobody could see the meaning of.
    private static func barHeight(for level: Double) -> CGFloat {
        let span = LevelHistory.ceiling - LevelHistory.silence
        let loud = (min(level, LevelHistory.ceiling) - LevelHistory.silence) / span
        return heights[min(heights.count - 1, Int(loud * Double(heights.count)))]
    }

    /// How one side is said out loud: how loud it is, or how long it has been
    /// silent. A screen reader gets no strip at all, so the sentence has to
    /// carry the thing the shape of the strip carries — that this side is not
    /// merely quiet, it has been quiet for long enough to be broken.
    static func spoken(_ side: String, _ history: LevelHistory, now: Date = Date()) -> String {
        if let quiet = history.silentFor(now: now) {
            return quiet < 1 ? "\(side) silent" : "\(side) silent for \(lasting(quiet))"
        }
        let level = Int(history.latest.rounded())
        return level >= 0 ? "\(side) 0 dB" : "\(side) −\(-level) dB"
    }

    /// The line under the meters in the menu, which always has something to
    /// say: how long a side has been silent, or else that both of them are
    /// being heard. Never blank, because the menu cannot change height while
    /// it is open and so the line is standing there either way — and a line
    /// standing there with nothing on it reads as something broken rather than
    /// as nothing to report.
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
///
/// The strip beside the mark has its own numbers and keeps them. Eight points
/// of menu bar and 248 points of panel are not the same problem, and a single
/// set of constants stretched over both would be wrong at one end or the other.
enum Waveform {
    /// A point of bar, a point of air, and a rounded cap on each end. At this
    /// pitch a talking voice reads as syllables rather than as a fence, and it
    /// is the narrowest a bar can be and still look drawn on purpose.
    static let barWidth: CGFloat = 2
    static let gap: CGFloat = 2
    static let pitch: CGFloat = barWidth + gap
    static let cap: CGFloat = 1

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

    /// The track the newest reading fills under Reduce Motion, on the same
    /// scale and at the same height as the one the strip falls back to.
    static let trackHeight: CGFloat = 4

    /// How wide the bars have to fit, and so how many of them there are. At ten
    /// readings a second the panel holds a little over six seconds of meeting —
    /// long enough to see a sentence in, short enough that what is on screen is
    /// still what is happening.
    static var span: CGFloat { RecordingPanelView.contentWidth - padding * 2 }
    static let columns = Int(span / pitch)

    /// Which whole even height a reading lands on, over the same −50 dBFS to 0
    /// the strip uses, everything below being silence with a shape of its own.
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
    /// than being dimmed the way the strip's is: the tray is already a lighter
    /// ground than the menu bar, and a dimmer line disappears into it.
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
/// The frames are set by hand rather than constrained. Nothing in here may
/// change size while somebody is watching it — a silence coming on, a device
/// with a longer name, a timer crossing an hour — because a panel that resizes
/// under the pointer is the one kind of movement there is no reading through.
/// That is also why the footer is a line that is always there and always says
/// something.
final class RecordingPanelView: NSVisualEffectView {
    /// Wide enough for a device name to survive beside its caption, narrow
    /// enough to hang off a status item without covering the menu bar's own
    /// items either side of it.
    static let width: CGFloat = 300
    static let inset: CGFloat = 16
    static var contentWidth: CGFloat { width - inset * 2 }

    /// Rounded at the bottom only. The top edge is flush against the menu bar,
    /// and a corner radius there would open a gap of desktop between the two
    /// that makes the panel a floating window rather than the status item
    /// having grown downwards.
    static let corner: CGFloat = 16

    /// How long the island takes to arrive, how long it takes to go, and how
    /// small it starts. Arriving is given nearly twice what going gets: the
    /// arrival is the part somebody watches, since it is what says this window
    /// came out of the status item above it, and a window that takes as long to
    /// leave is a window standing in the way of whatever dismissed it.
    ///
    /// Four per cent is a long way short of a zoom — twelve points across a
    /// panel this wide — because the thing being shown is where the window came
    /// from, not that it made an entrance.
    static let appearing: TimeInterval = 0.22
    static let vanishing: TimeInterval = 0.12
    private static let arrivingFrom: CGFloat = 0.96

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
        material = .hudWindow
        state = .active
        blendingMode = .behindWindow
        appearance = NSAppearance(named: .darkAqua)
        wantsLayer = true
        layer?.cornerRadius = RecordingPanelView.corner
        // minY is the bottom in this coordinate space, so these two are the
        // bottom corners and the top edge stays square against the menu bar
        layer?.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]
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
    private static func scaled(_ scale: CGFloat, about bounds: CGRect) -> CATransform3D {
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

// MARK: - end of meters and marks

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// accessory: this app is its menu bar item and nothing else — no Dock tile, no
// window, and no place in the app switcher for something with nothing to show
app.setActivationPolicy(.accessory)
app.run()
