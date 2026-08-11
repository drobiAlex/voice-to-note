// vtn-capture — records a meeting on this Mac as two wav files: everything the
// Mac is playing (the other people in the call) and the microphone (whoever is
// sitting here). The two are kept apart rather than mixed so that the pipeline
// downstream can tell one side of a conversation from the other; merging them
// is ffmpeg's job, not this program's.
//
// Recording runs until SIGINT or SIGTERM. Its parent reads two words on stdout:
// "recording" once both streams are live, "stopped" once both files are closed.

import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin
import Foundation

let usage = "usage: vtn-capture <system.wav> <mic.wav>"

enum Exit {
    static let usage: Int32 = 2
    static let microphone: Int32 = 3
    static let systemAudio: Int32 = 4
}

struct CaptureError: LocalizedError {
    let message: String

    init(_ message: String) {
        self.message = message
    }

    var errorDescription: String? { message }
}

/// Says one word to whatever launched this, now rather than whenever the pipe
/// happens to flush: the parent waits on "recording" before it tells anyone the
/// meeting is being taped.
func say(_ line: String) {
    print(line)
    fflush(stdout)
}

func warn(_ line: String) {
    FileHandle.standardError.write(Data((line + "\n").utf8))
}

func die(_ line: String, _ code: Int32) -> Never {
    warn(line)
    exit(code)
}

/// Turns a Core Audio status code into something a person can act on, naming
/// the step that failed rather than only the number it failed with.
func check(_ status: OSStatus, _ what: String) throws {
    guard status == noErr else {
        throw CaptureError("\(what) failed (OSStatus \(status))")
    }
}

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
}

/// The device the Mac is playing through right now. The tap is hung off this
/// one, so a meeting on headphones is recorded as readily as one on speakers.
func defaultOutputDevice() throws -> AudioObjectID {
    var selector = address(kAudioHardwarePropertyDefaultOutputDevice)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    try check(
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &selector, 0, nil, &size, &device
        ),
        "finding the default output device"
    )
    guard device != AudioObjectID(kAudioObjectUnknown) else {
        throw CaptureError("this Mac has no audio output device to record")
    }
    return device
}

func deviceUID(_ device: AudioObjectID) throws -> String {
    var selector = address(kAudioDevicePropertyDeviceUID)
    var uid: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(device, &selector, 0, nil, &size, $0)
    }
    try check(status, "identifying the default output device")
    return uid as String
}

/// A wav file to write one stream into, in that stream's own format. AVAudioFile
/// owns the RIFF header — it is written short at the start and corrected when
/// the file is closed, which is why nothing here hand-rolls one.
func wavFile(at url: URL, like format: AVAudioFormat) throws -> AVAudioFile {
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: format.sampleRate,
        AVNumberOfChannelsKey: format.channelCount,
        AVLinearPCMBitDepthKey: 32,
        AVLinearPCMIsFloatKey: true,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ]
    return try AVAudioFile(
        forWriting: url,
        settings: settings,
        commonFormat: .pcmFormatFloat32,
        interleaved: format.isInterleaved
    )
}

/// Records everything this Mac is playing, by tapping the system-wide output mix
/// and wrapping that tap in a private aggregate device only this process sees.
final class SystemAudioRecorder {
    private let url: URL
    private let queue = DispatchQueue(label: "app.vtn.capture.system")
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private var file: AVAudioFile?
    private var reportedWriteFailure = false

    init(url: URL) {
        self.url = url
    }

    func start() throws {
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        // private: the tap belongs to this process alone, so it never shows up
        // as a device anybody else could pick or record through
        description.isPrivate = true
        try check(AudioHardwareCreateProcessTap(description, &tap), "creating the audio tap")

        let outputUID = try deviceUID(try defaultOutputDevice())
        let settings: [String: Any] = [
            kAudioAggregateDeviceNameKey: "vtn-capture",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            // the real output device, not a placeholder: an aggregate built
            // around a device that is not playing hands back nothing but silence
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: description.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]
            ],
        ]
        try check(
            AudioHardwareCreateAggregateDevice(settings as CFDictionary, &aggregate),
            "creating the capture device"
        )

        // the tap follows whatever rate and channel count the output device is
        // set to, and a person can change that between meetings — so the format
        // is read back here rather than assumed
        var asbd = try tapFormat()
        guard let format = AVAudioFormat(streamDescription: &asbd) else {
            throw CaptureError("this Mac's audio is in a format that cannot be recorded")
        }
        file = try wavFile(at: url, like: format)

        let status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, queue) {
            [weak self] _, input, _, _, _ in
            guard let self, let file = self.file else { return }
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input) else {
                return
            }
            do {
                try file.write(from: buffer)
            } catch {
                self.reportWriteFailure(error)
            }
        }
        try check(status, "listening to the capture device")
        try check(AudioDeviceStart(aggregate, proc), "starting the capture device")
    }

    /// Teardown runs strictly inside out: the device has to stop calling back
    /// before what the callback writes into goes away, and the tap outlives the
    /// aggregate device built around it. Out of order, this crashes or leaves a
    /// tap behind that the next run cannot replace.
    func stop() {
        if let proc {
            AudioDeviceStop(aggregate, proc)
            AudioDeviceDestroyIOProcID(aggregate, proc)
            self.proc = nil
        }
        if aggregate != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyAggregateDevice(aggregate)
            aggregate = AudioObjectID(kAudioObjectUnknown)
        }
        if tap != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tap)
            tap = AudioObjectID(kAudioObjectUnknown)
        }
        // releasing the file is what finalizes its header
        file = nil
    }

    private func tapFormat() throws -> AudioStreamBasicDescription {
        var selector = address(kAudioTapPropertyFormat)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check(
            AudioObjectGetPropertyData(tap, &selector, 0, nil, &size, &asbd),
            "reading the audio tap's format"
        )
        return asbd
    }

    /// Said once and not again: a stream that cannot be written fails on every
    /// buffer, and a meeting's worth of identical lines would bury it.
    private func reportWriteFailure(_ error: Error) {
        guard !reportedWriteFailure else { return }
        reportedWriteFailure = true
        warn("system audio is not being written: \(error.localizedDescription)")
    }
}

/// Records the default microphone — whoever is in the room — through the input
/// node's own format, whatever the device happens to offer.
final class MicrophoneRecorder {
    private let url: URL
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var reportedWriteFailure = false

    init(url: URL) {
        self.url = url
    }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CaptureError("this Mac has no microphone to record")
        }
        file = try wavFile(at: url, like: format)
        input.installTap(onBus: 0, bufferSize: 4096, format: nil) { [weak self] buffer, _ in
            guard let self, let file = self.file else { return }
            do {
                try file.write(from: buffer)
            } catch {
                self.reportWriteFailure(error)
            }
        }
        try engine.start()
    }

    func stop() {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        file = nil
    }

    private func reportWriteFailure(_ error: Error) {
        guard !reportedWriteFailure else { return }
        reportedWriteFailure = true
        warn("microphone audio is not being written: \(error.localizedDescription)")
    }
}

/// Asks for the microphone and waits for the answer. The first run puts the
/// system's own prompt on screen; every run after that is answered from what
/// the person said then.
func microphoneAllowed() -> Bool {
    if AVCaptureDevice.authorizationStatus(for: .audio) == .authorized {
        return true
    }
    let answered = DispatchSemaphore(value: 0)
    var allowed = false
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        allowed = granted
        answered.signal()
    }
    answered.wait()
    return allowed
}

let arguments = CommandLine.arguments
guard arguments.count == 3 else {
    die(usage, Exit.usage)
}

let systemAudio = SystemAudioRecorder(url: URL(fileURLWithPath: arguments[1]))
let microphone = MicrophoneRecorder(url: URL(fileURLWithPath: arguments[2]))

guard microphoneAllowed() else {
    die(
        "microphone access denied — allow vtn-capture under"
            + " System Settings → Privacy & Security → Microphone",
        Exit.microphone
    )
}

// system audio has no permission to ask for up front: the attempt is the check,
// and the system puts its own prompt up the first time one is made
do {
    try systemAudio.start()
} catch {
    systemAudio.stop()
    die(
        "cannot record this Mac's audio (\(error.localizedDescription)) — allow vtn-capture"
            + " under System Settings → Privacy & Security → Screen & System Audio Recording",
        Exit.systemAudio
    )
}

do {
    try microphone.start()
} catch {
    systemAudio.stop()
    microphone.stop()
    die("cannot record the microphone (\(error.localizedDescription))", Exit.microphone)
}

say("recording")

func stopEverything() -> Never {
    systemAudio.stop()
    microphone.stop()
    say("stopped")
    exit(0)
}

// the default handlers would kill this process where it stands, leaving two wav
// files without finished headers; ignoring them hands the signal to the sources
// below instead, which stop the recording properly
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let stopSignals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { stopEverything() }
    source.resume()
    return source
}

dispatchMain()
