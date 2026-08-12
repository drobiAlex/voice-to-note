// vtn-capture — records a meeting on this Mac as two wav files: everything the
// Mac is playing (the other people in the call) and the microphone (whoever is
// sitting here). The two are kept apart rather than mixed so that the pipeline
// downstream can tell one side of a conversation from the other; merging them
// is ffmpeg's job, not this program's.
//
// Recording runs until SIGINT or SIGTERM. Its parent reads two words on stdout:
// "recording" once both streams are live, "stopped" once both files are closed.
//
// Either side can be pointed at a particular device by UID. Named neither way,
// it records the whole system mix and the default microphone — what somebody
// who has not been asked to choose would expect. --list-devices prints what
// there is to choose from and records nothing.

import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin
import Foundation

let usage = """
    usage: vtn-capture <system.wav> <mic.wav> [--output-uid <UID>] [--input-uid <UID>]
           vtn-capture --list-devices
    """

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
/// one unless the command line named another, so a meeting on headphones is
/// recorded as readily as one on speakers.
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
    try check(status, "identifying an audio device")
    return uid as String
}

/// What the command line asked for. Both files are required; a side left
/// unnamed is recorded from whatever this Mac is set to use, which is what
/// makes an invocation with no flags behave as it always has.
struct Options {
    var system = ""
    var mic = ""
    var outputUID: String?
    var inputUID: String?
}

/// Reads the command line, or refuses it. Flags may come in any order and on
/// either side of the two file names; anything else beginning with a dash is a
/// typo rather than something to guess at, since guessing wrong here costs a
/// meeting that was never taped.
func parseOptions(_ argv: [String]) -> Options {
    var options = Options()
    var files: [String] = []
    var i = 1
    while i < argv.count {
        let argument = argv[i]
        switch argument {
        case "--output-uid", "--input-uid":
            guard i + 1 < argv.count else { die(usage, Exit.usage) }
            if argument == "--output-uid" {
                options.outputUID = argv[i + 1]
            } else {
                options.inputUID = argv[i + 1]
            }
            i += 2
        default:
            guard !argument.hasPrefix("-") else { die(usage, Exit.usage) }
            files.append(argument)
            i += 1
        }
    }
    guard files.count == 2 else { die(usage, Exit.usage) }
    options.system = files[0]
    options.mic = files[1]
    return options
}

/// Every audio device attached to this Mac.
func allDevices() throws -> [AudioObjectID] {
    var selector = address(kAudioHardwarePropertyDevices)
    var size = UInt32(0)
    try check(
        AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject), &selector, 0, nil, &size
        ),
        "sizing this Mac's list of audio devices"
    )
    var devices = [AudioObjectID](
        repeating: AudioObjectID(kAudioObjectUnknown),
        count: Int(size) / MemoryLayout<AudioObjectID>.size
    )
    guard !devices.isEmpty else { return [] }
    try check(
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &selector, 0, nil, &size, &devices
        ),
        "reading this Mac's list of audio devices"
    )
    return devices
}

/// The device a UID names, or nothing if it names none. A UID is chosen once
/// in a picker and used for meetings afterwards, by which time the headphones
/// it named may be in a drawer — so the answer here is routinely nothing.
func device(withUID uid: String) -> AudioObjectID? {
    let devices = (try? allDevices()) ?? []
    return devices.first { (try? deviceUID($0)) == uid }
}

/// What a person calls this device, as distinct from the UID a machine knows
/// it by. A device whose name cannot be read is still worth listing, since its
/// UID is the part that has to be right.
func deviceName(_ device: AudioObjectID) -> String {
    var selector = address(kAudioObjectPropertyName)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(device, &selector, 0, nil, &size, $0)
    }
    return status == noErr ? name as String : "unnamed device"
}

/// How many channels a device carries in one direction. None at all is what
/// tells a microphone apart from a pair of speakers; a headset answers in both
/// directions and is genuinely both.
func channelCount(_ device: AudioObjectID, _ scope: AudioObjectPropertyScope) -> UInt32 {
    var selector = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: scope,
        mElement: kAudioObjectPropertyElementMain
    )
    var size = UInt32(0)
    guard AudioObjectGetPropertyDataSize(device, &selector, 0, nil, &size) == noErr, size > 0
    else { return 0 }
    // an AudioBufferList is variably long — one buffer per stream — so it has
    // to be read into memory sized by the answer above rather than a value
    let raw = UnsafeMutableRawPointer.allocate(
        byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment
    )
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(device, &selector, 0, nil, &size, raw) == noErr else {
        return 0
    }
    let buffers = UnsafeMutableAudioBufferListPointer(
        raw.assumingMemoryBound(to: AudioBufferList.self)
    )
    return buffers.reduce(0) { $0 + $1.mNumberChannels }
}

/// How a device is attached to this Mac — built in, USB, Bluetooth and so on.
/// A device that will not answer is reported as unknown rather than guessed at,
/// which leaves the caller below preferring one that did answer.
func transportType(_ device: AudioObjectID) -> UInt32 {
    var selector = address(kAudioDevicePropertyTransportType)
    var transport = UInt32(kAudioDeviceTransportTypeUnknown)
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(device, &selector, 0, nil, &size, &transport) == noErr
    else { return UInt32(kAudioDeviceTransportTypeUnknown) }
    return transport
}

/// This Mac's own speakers, or nothing on a Mac that has none — a mini driving
/// a monitor's audio over HDMI has none. Wanted not for what they play but for
/// how steadily they tick: see the clock the capture aggregate is built around.
func builtInOutputUID() -> String? {
    let devices = (try? allDevices()) ?? []
    for device in devices
    where transportType(device) == UInt32(kAudioDeviceTransportTypeBuiltIn)
        && channelCount(device, kAudioObjectPropertyScopeOutput) > 0 {
        if let uid = try? deviceUID(device) { return uid }
    }
    return nil
}

/// Prints what a picker can offer: one tab-separated `direction UID name` line
/// per direction a device works in. Nothing here opens a tap, an aggregate or
/// a file, and macOS asks the user for nothing — listing devices is not
/// recording, so a picker can be filled in long before permission is granted.
func listDevices() {
    guard let devices = try? allDevices() else {
        die("cannot read this Mac's audio devices", Exit.systemAudio)
    }
    for device in devices {
        guard let uid = try? deviceUID(device) else { continue }
        let name = deviceName(device)
        if channelCount(device, kAudioObjectPropertyScopeInput) > 0 {
            say("in\t\(uid)\t\(name)")
        }
        if channelCount(device, kAudioObjectPropertyScopeOutput) > 0 {
            say("out\t\(uid)\t\(name)")
        }
    }
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

/// Records what this Mac is playing, by tapping its output and wrapping that
/// tap in a private aggregate device only this process sees. Named an output
/// device, it hears that one alone; named none, it hears the system-wide mix.
final class SystemAudioRecorder {
    private let url: URL
    private let requestedUID: String?
    private let queue = DispatchQueue(label: "app.vtn.capture.system")
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private var file: AVAudioFile?
    private var reportedWriteFailure = false

    init(url: URL, deviceUID requestedUID: String?) {
        self.url = url
        self.requestedUID = requestedUID
    }

    func start() throws {
        let outputUID = try requestedUID ?? deviceUID(defaultOutputDevice())
        // a tap scoped to one device hears only what is played through it,
        // where the global tap would also carry whatever is going to the Mac's
        // other outputs — a meeting on headphones plus music on the speakers
        let description =
            requestedUID == nil
            ? CATapDescription(stereoGlobalTapButExcludeProcesses: [])
            : CATapDescription(excludingProcesses: [], deviceUID: outputUID, stream: 0)
        // private: the tap belongs to this process alone, so it never shows up
        // as a device anybody else could pick or record through
        description.isPrivate = true
        try check(AudioHardwareCreateProcessTap(description, &tap), "creating the audio tap")

        // the sub-device below is the aggregate's clock, not the source of what
        // is recorded: a global tap carries the mix of every process whichever
        // device happens to be playing it, so this only has to tick steadily.
        // Bluetooth does not. Its rate is renegotiated the moment any app opens
        // the headset's microphone — the call that is being recorded does
        // exactly that — and an aggregate never re-reads the device it was built
        // around, so the IOProc goes on firing and hands back zeroed buffers for
        // the rest of the meeting. This Mac's own speakers are always attached
        // and never renegotiate, which is what makes them the clock to prefer;
        // a Mac without any falls back to whatever it is playing through.
        // A tap scoped to one device is left clocked by that device: that device
        // is the one being recorded, so its silence would be the truth.
        let clockUID = requestedUID == nil ? builtInOutputUID() ?? outputUID : outputUID

        let settings: [String: Any] = [
            kAudioAggregateDeviceNameKey: "vtn-capture",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: clockUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: clockUID]],
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

/// Records the microphone — whoever is in the room — through the input node's
/// own format, whatever the device happens to offer. Named a device, it
/// records that one; named none, whichever the Mac is set to listen through.
final class MicrophoneRecorder {
    private let url: URL
    private let requestedUID: String?
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var reportedWriteFailure = false

    init(url: URL, deviceUID requestedUID: String?) {
        self.url = url
        self.requestedUID = requestedUID
    }

    func start() throws {
        let input = engine.inputNode
        if let requestedUID {
            try listen(through: requestedUID, on: input)
        }
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

    /// Binds the engine to one particular microphone, through the input node's
    /// own audio unit. Has to happen before the node's format is read and its
    /// tap installed: the node describes whichever device it is bound to at
    /// the time, and a tap fitted to the previous one records silence.
    private func listen(through uid: String, on input: AVAudioInputNode) throws {
        guard var chosen = device(withUID: uid) else {
            throw CaptureError("no microphone with UID \(uid)")
        }
        guard let unit = input.audioUnit else {
            throw CaptureError("this Mac's audio engine offers no input to choose a device for")
        }
        try check(
            AudioUnitSetProperty(
                unit,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                0,
                &chosen,
                UInt32(MemoryLayout<AudioDeviceID>.size)
            ),
            "listening through the microphone \(uid)"
        )
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

// listing devices is answered before anything else is set up, because it is
// the one thing this program does that records nothing
if arguments.count == 2, arguments[1] == "--list-devices" {
    listDevices()
    exit(0)
}

let options = parseOptions(arguments)

// a UID was chosen in a picker at some earlier point, and the device behind it
// can be unplugged between meetings. Both are checked here, ahead of the first
// permission prompt, so a stale choice is named at once rather than after
// somebody has answered macOS and sat down to talk.
if let uid = options.outputUID, device(withUID: uid) == nil {
    die("no audio output device with UID \(uid) — see: vtn devices", Exit.systemAudio)
}
if let uid = options.inputUID, device(withUID: uid) == nil {
    die("no microphone with UID \(uid) — see: vtn devices", Exit.microphone)
}

let systemAudio = SystemAudioRecorder(
    url: URL(fileURLWithPath: options.system), deviceUID: options.outputUID
)
let microphone = MicrophoneRecorder(
    url: URL(fileURLWithPath: options.mic), deviceUID: options.inputUID
)

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
