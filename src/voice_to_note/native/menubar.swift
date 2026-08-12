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

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        show()
        askToNotify()
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
    /// being made until its process is gone.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        switch state {
        case .idle:
            menu.addItem(action("Start Recording", #selector(startRecording)))
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
        recorder.arguments = ["record"]
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
