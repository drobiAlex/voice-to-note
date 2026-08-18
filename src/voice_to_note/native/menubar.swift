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
            // the meters belong to one recording for the same reason, and
            // emptying them on every state that is not recording is also what
            // makes each recording start from silence rather than from
            // whatever the last one ended on
            if state != .recording {
                forgetMeters()
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
    private var meterView: MeterMenuView?

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
        let menu = NSMenu()
        menu.delegate = self
        // this menu says for itself what can be chosen — its own state decides
        // that. Left to AppKit, a picker whose list has not arrived yet would be
        // greyed out and unopenable, which tells the person nothing at all
        menu.autoenablesItems = false
        statusItem.menu = menu
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
        meterView?.show(system: systemLevels, microphone: microphoneLevels)
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
        meterView?.showFooter(system: systemLevels, microphone: microphoneLevels, now: now)
    }

    /// Everything the meters were made of, dropped. The menu's view goes with
    /// the readings: it was built around the devices this recording used, and
    /// the next recording may be listening to something else entirely.
    private func forgetMeters() {
        systemLevels.reset()
        microphoneLevels.reset()
        meterView = nil
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
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        switch state {
        case .idle:
            menu.addItem(action("Start Recording", #selector(startRecording)))
            menu.addItem(.separator())
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
            if state == .recording {
                menu.addItem(meterItem())
                menu.addItem(.separator())
            }
            menu.addItem(action("Stop Recording", #selector(stopRecording)))
            menu.addItem(note(state == .recording ? "Recording \(elapsed())" : "Starting …"))
        case .processing:
            menu.addItem(note("Processing memo …"))
        }
        menu.addItem(.separator())
        menu.addItem(action("Quit", #selector(quit)))
    }

    /// The meters, at the top of the menu because checking them is what the
    /// menu is opened for mid-meeting. The view is built once per recording and
    /// handed to every opening after that: rebuilding it here would throw away
    /// the readings arriving while somebody watches it. Like everything else in
    /// this menu it runs no subprocess — a menu is drawn on the main thread,
    /// and a menu that waits on `vtn` is a menu that does not open.
    private func meterItem() -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.isEnabled = false
        let view = meterView ?? MeterMenuView(
            system: chosenOutput?.name ?? "system mix",
            microphone: chosenInput?.name ?? "default microphone"
        )
        meterView = view
        view.show(system: systemLevels, microphone: microphoneLevels)
        view.showFooter(system: systemLevels, microphone: microphoneLevels)
        item.view = view
        return item
    }

    private func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        return item
    }

    /// A line that says what is happening rather than offering to do anything.
    private func note(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
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
    init(columns: Int = Meters.columns) {
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
    static func composite(
        mark: NSImage, system: LevelHistory, microphone: LevelHistory, reduceMotion: Bool
    ) -> NSImage {
        let top = system.trace
        let bottom = microphone.trace
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

/// The meters as the menu shows them: the control System Settings puts under
/// a microphone, one per side, each named with the device it is listening to.
/// The menu is where somebody goes to find out which of the two is dead, and
/// the reading alone cannot say that — "Microphone · AirPods Pro" is what
/// turns a flat meter into something that can be fixed.
///
/// Built once per recording and kept. A menu item's view is measured rather
/// than laid out, so the frames in here are set by hand and must not depend
/// on the text in them: a silence coming on while the menu is open cannot
/// resize the window the menu is already in, which is why the footer under
/// the meters is a line that is always there and always says something.
final class MeterMenuView: NSView {
    /// A menu item's own text starts 14 points in, and matching that is what
    /// keeps this from reading as a panel that fell into a menu.
    private static let inset: CGFloat = 14
    private static let spacing: CGFloat = 6

    /// The meters are as wide as the two captions need and no wider. Narrower
    /// than this a level indicator is a smear rather than a reading; wider than
    /// this and a menu has stopped being a menu. Between the two the device
    /// name wins, because a name cut off at "MacBook Pr…" gives back the very
    /// thing the caption is here for.
    private static let narrowest: CGFloat = 160
    private static let widest: CGFloat = 260

    /// A caption belongs to the meter under it, so it sits nearer to that
    /// than the rows sit to each other.
    private static let tight: CGFloat = 2
    private static let captionHeight: CGFloat = 14
    private static let indicatorHeight: CGFloat = 12

    /// Fixed, unlike the width: three lines of caption and two meters, the
    /// third caption being the footer, which is standing there whatever the
    /// two sides are doing.
    private static let height =
        spacing * 4 + tight * 2 + captionHeight * 3 + indicatorHeight * 2

    private let size: NSSize
    private let systemCaption: NSTextField
    private let systemLevel: NSLevelIndicator
    private let microphoneCaption: NSTextField
    private let microphoneLevel: NSLevelIndicator
    private let footer: NSTextField

    /// The devices are taken at the start of a recording and never asked for
    /// again: they are what this recording was started with, and a device
    /// picked in the menu afterwards changes nothing about the tape running.
    init(system: String, microphone: String) {
        systemCaption = MeterMenuView.caption("System audio · \(system)")
        systemLevel = MeterMenuView.indicator()
        microphoneCaption = MeterMenuView.caption("Microphone · \(microphone)")
        microphoneLevel = MeterMenuView.indicator()
        footer = MeterMenuView.caption("")
        let wanted = max(systemCaption.fittingSize.width, microphoneCaption.fittingSize.width)
        size = NSSize(
            width: MeterMenuView.inset * 2
                + min(max(wanted, MeterMenuView.narrowest), MeterMenuView.widest),
            height: MeterMenuView.height
        )
        super.init(frame: NSRect(origin: .zero, size: size))
        var top = size.height - MeterMenuView.spacing
        for (caption, level) in [
            (systemCaption, systemLevel), (microphoneCaption, microphoneLevel),
        ] {
            caption.frame = place(top, MeterMenuView.captionHeight)
            top -= MeterMenuView.captionHeight + MeterMenuView.tight
            level.frame = place(top, MeterMenuView.indicatorHeight)
            top -= MeterMenuView.indicatorHeight + MeterMenuView.spacing
            addSubview(caption)
            addSubview(level)
        }
        footer.frame = place(top, MeterMenuView.captionHeight)
        addSubview(footer)
    }

    /// This view is only ever built in code; there is no nib in this app for
    /// one to be loaded from.
    required init?(coder: NSCoder) {
        fatalError("MeterMenuView is built in code, not loaded from a nib")
    }

    /// A row of the given height hanging from `top`, inset the way a menu
    /// insets its own text.
    private func place(_ top: CGFloat, _ height: CGFloat) -> NSRect {
        NSRect(
            x: MeterMenuView.inset, y: top - height,
            width: size.width - MeterMenuView.inset * 2, height: height
        )
    }

    /// What a menu asks a view for when it is deciding how wide to be. It is
    /// the size the frame was built at, because both are the same fixed
    /// layout and a menu that measured one and drew the other would clip.
    override var intrinsicContentSize: NSSize {
        size
    }

    /// The newest reading on each side. Setting this on a level indicator
    /// inside a menu that is already open is exactly the point: the readings
    /// arrive on the main queue as the recorder prints them, ten a second, so
    /// the meters move under the pointer of whoever is watching them.
    func show(system: LevelHistory, microphone: LevelHistory) {
        systemLevel.doubleValue = MeterMenuView.shown(system.latest)
        microphoneLevel.doubleValue = MeterMenuView.shown(microphone.latest)
    }

    /// The line under the meters, refreshed once a second rather than with
    /// every reading: it counts in whole seconds, and nothing in it can change
    /// ten times between two of them.
    func showFooter(system: LevelHistory, microphone: LevelHistory, now: Date = Date()) {
        footer.stringValue = Meters.footer(system: system, microphone: microphone, now: now)
    }

    /// Small, secondary and one line: the type a menu uses for what it is
    /// saying rather than offering. Truncated at the tail because a device is
    /// recognised from the start of its name — "MacBook Pro Micro…" is still
    /// the built-in microphone.
    private static func caption(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        field.textColor = .secondaryLabelColor
        field.maximumNumberOfLines = 1
        field.lineBreakMode = .byTruncatingTail
        return field
    }

    /// The same control, on the same scale, that System Settings shows under
    /// a microphone — which is the whole reason for it: nobody has to learn
    /// what this one means. Not editable, because it reports rather than sets.
    private static func indicator() -> NSLevelIndicator {
        let level = NSLevelIndicator()
        level.levelIndicatorStyle = .continuousCapacity
        level.minValue = LevelHistory.floor
        level.maxValue = LevelHistory.ceiling
        // where a recording stops having headroom and where it starts losing
        // the loud parts of a voice altogether
        level.warningValue = -12
        level.criticalValue = -3
        level.isEditable = false
        level.doubleValue = LevelHistory.floor
        return level
    }

    /// Clamped to the scale the indicator was given, so a reading off either
    /// end of it is drawn as that end rather than as nothing at all.
    private static func shown(_ level: Double) -> Double {
        min(max(level, LevelHistory.floor), LevelHistory.ceiling)
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
