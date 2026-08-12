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

    private var state = RecorderState.idle
    private var recorder: Process?
    private var errors: Pipe?
    private var unread = ""
    private var startedAt: Date?
    private var ticker: Timer?
    private var mayNotify = false
    private var logFile: FileHandle?
    private var projects: [(name: String, count: Int)]?
    private var devices: AudioDevices?
    private var refreshing = false

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
            button.image = template("record.circle", "Not recording")
            button.attributedTitle = NSAttributedString(string: "")
        case .starting:
            button.image = template("record.circle", "Starting to record")
            button.attributedTitle = label("…")
        case .recording:
            button.image = redDot()
            button.attributedTitle = label(elapsed())
        case .processing:
            button.image = template("waveform", "Making notes from the meeting")
            button.attributedTitle = NSAttributedString(string: "")
        }
    }

    private func template(_ symbol: String, _ description: String) -> NSImage? {
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: description)
        image?.isTemplate = true
        return image
    }

    /// The one thing on screen saying a meeting is being taped, so it keeps its
    /// colour instead of being tinted to match the menu bar the way every other
    /// icon here is.
    private func redDot() -> NSImage? {
        let image = NSImage(systemSymbolName: "record.circle.fill", accessibilityDescription: "Recording")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(hierarchicalColor: .systemRed))
        image?.isTemplate = false
        return image
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
            menu.addItem(action("Stop Recording", #selector(stopRecording)))
            menu.addItem(note(state == .recording ? "Recording \(elapsed())" : "Starting …"))
        case .processing:
            menu.addItem(note("Processing memo …"))
        }
        menu.addItem(.separator())
        menu.addItem(action("Quit", #selector(quit)))
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
    private func recordArguments() -> [String] {
        var arguments = ["record", "--project", chosenProject]
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

    /// Everything the recorder says on its way through a meeting, kept whole in
    /// the log and read for the one line that matters here: it is printed only
    /// once both audio streams are live, so it is what the red dot waits for.
    private func heard(_ data: Data) {
        unread += String(decoding: data, as: UTF8.self)
        while let newline = unread.firstIndex(of: "\n") {
            let line = String(unread[..<newline])
            unread = String(unread[unread.index(after: newline)...])
            write(line)
            if state == .starting, line.hasPrefix("recording —") {
                startedAt = Date()
                state = .recording
                startTicking()
                show()
            }
        }
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

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// accessory: this app is its menu bar item and nothing else — no Dock tile, no
// window, and no place in the app switcher for something with nothing to show
app.setActivationPolicy(.accessory)
app.run()
