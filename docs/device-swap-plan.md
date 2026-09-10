# Changing the microphone or the sound source mid-meeting

A plan for the roadmap item of that name. Written so that somebody who has not read
`capture.swift` can tell what is being changed and why each step exists before they start.

## Why this is worth building

A recording is set up once and then runs for an hour. Every device decision is made in the
first ten seconds, when nobody yet knows that the headset battery is at four per cent, that
the call will move from speakers to AirPods, or that the dock will be unplugged. When one
of those happens the tape does not stop — it keeps rolling on a device that has gone, and
what lands in the memo is an hour of one-sided silence that nobody notices until the
transcript is read.

The recorder already knows: the puck shows *Microphone silent 12 s* in amber, and the
island's footer says the same. That is a diagnosis with no cure. This is the cure.

The larger half of the prize needs no user interface at all: **a recorder that can change
its own device can notice one disappearing and move itself**, which is the case that
actually happens.

## The idea in one sentence

A device swap is a chunk rollover that happens to open the new piece on a different device
— so the work is mostly the rollover already on the roadmap, and the swap is what it
unlocks.

## What exists today

So that the steps below are changes rather than inventions:

- **`capture.swift`** runs two independent recorders. `SystemAudioRecorder` builds a
  `CATapDescription` — global, or scoped to one output device — wraps it in a private
  aggregate device and writes what its IOProc receives. `MicrophoneRecorder` runs an
  `AVAudioEngine`, taps `inputNode`, and writes what the tap delivers. Each picks its
  device once, in `start()`.
- **The microphone is bound** by setting `kAudioOutputUnitProperty_CurrentDevice` on the
  input node's audio unit, and the ordering constraint is already documented there: it
  "has to happen before the node's format is read and its tap installed: the node
  describes whichever device it is bound to at the time, and a tap fitted to the previous
  one records silence." That sentence is the whole of step 3.
- **The protocol** is one-way. The helper says `recording`, then `level<TAB>sys<TAB>mic`
  ten times a second when asked, then `stopped`. Its stdin is unused.
- **Stopping** is SIGINT or SIGTERM, handled by `stopEverything()`, which silences the
  meter, closes both files and says `stopped` last.
- **Python** reads the two growing wavs by their size on disk (`capture.TrackReader`),
  transcribes stretches of them while the meeting runs (`services.LiveSession`), and
  merges the two files with ffmpeg at the end (`gateways.audio`).
- **The menu bar app** launches `vtn record --levels …` and reads its stderr. Its device
  pickers go quiet the moment the tape rolls, deliberately: a choice made then would
  silently be a choice for the *next* recording.

## The steps

### Step 0 — Prove a live rebind is possible, before designing around it

A throwaway branch: record the microphone, and five seconds in, stop the engine, rebind it
to a second device, open a second file and start again. Print the gap between the last
frame of one file and the first of the next.

*Why first:* every step after this assumes a rebind works and costs a fraction of a
second. If `AVAudioEngine` needs longer than about a quarter second, the design changes —
two engines overlapping rather than one restarted — and it is much cheaper to learn that
now than after the protocol and the merge have been built around the wrong shape.

*Done when:* two pieces exist, both audible, and the gap is a measured number rather than
a hope.

### Step 1 — Chunk rollover (the prerequisite already on the roadmap)

`capture.swift` closes and reopens its pair of files on a cadence and announces each
finished pair on the protocol it already speaks: `piece<TAB>n<TAB>system-path<TAB>mic-path`.

*Why:* a WAV header states the sample rate and the channel count, and is written when the
file is closed. A device with a different format cannot continue a file that already
claims another. Rollover turns a format change from corruption into a file boundary. It
also retires the byte-cursor reading in `TrackReader`, which exists only because these
files are read while they are being written.

*Done when:* an ordinary recording produces several pairs, `ffprobe` is happy with each,
and the transcript is unchanged.

### Step 2 — A command channel on stdin

The helper reads lines from stdin: `input <uid>`, `output <uid>`, either with `default` in
place of a UID. Every command is answered — `switched<TAB>side<TAB>uid<TAB>name`, or
`refused<TAB>side<TAB>reason` — so nothing that asks has to guess whether it worked.

*Why stdin:* the pipe already exists between `vtn record` and the helper. A socket, a
control file or a signal would each need something new to exist, be found, and be cleaned
up; stdin needs none of that and dies with the process.

*Details that matter:* the reader runs on its own queue and never on an audio thread —
a real-time callback that blocks on a pipe is a hole in the recording. Unknown lines are
ignored rather than fatal, so a newer front end can talk to an older helper.

### Step 3 — The swap itself

On `input`: close the current microphone piece, stop the engine, remove the tap, rebind
the unit to the new device, read the format it now reports, open a new piece in that
format, reinstall the tap, start. On `output`: destroy the IOProc, the aggregate device
and the tap, rebuild all three for the new device, open a new piece.

*Why that order:* it is the order the existing comment demands — bind, then read the
format, then tap. Any other order records silence.

*Invariants to hold:* the `LevelMeter` objects are reused rather than rebuilt, so the
meters in the menu bar do not blink out and the silence clock is not reset by a swap. A
swap that fails leaves the old device recording and answers `refused` — nothing about
changing a device may end a meeting. A swap of one side never touches the other.

### Step 4 — A device that vanishes

A Core Audio property listener on the device list and on the default device. When the
device being recorded disappears, fall back to the default and announce the swap with its
reason.

*Why this is the important step:* it is the case that actually happens, and it is the one
that needs no interface at all. Falling back automatically can never be worse than
recording silence — that is the design decision, and it should be made explicitly rather
than by omission.

### Step 5 — Merge across pieces

`gateways.audio` concatenates each side's pieces before merging the two sides as it does
now.

*Why it is not the obvious ffmpeg one-liner:* the concat *demuxer* requires identical
formats, and the join is at a device switch — precisely where the formats differ. It wants
the concat *filter* with a resample per input.

*Done when:* a recording whose two pieces have different sample rates merges into one file
whose duration is the sum of theirs, to within a frame or two.

### Step 6 — Live transcription across pieces

`services.LiveSession` consumes the announced pieces instead of tracking byte cursors into
files that are still being written.

*Why:* rollover was proposed for this reason alone; a swap makes it compulsory, because a
cursor into a file that has been closed and replaced points at nothing.

### Step 7 — The surfaces

`vtn record` relays lines from its own stdin to the helper and prints what comes back. The
menu bar app keeps its device pickers live while the tape rolls and writes the chosen UID
to `vtn record`'s stdin. The puck's two device choosers stay enabled during a recording;
the project chooser does not, because a memo's project is settled when it starts. The
island's device captions, fixed at the start of a recording today, follow the swap.

*Why the pickers were disabled in the first place:* so that a choice made mid-tape could
not silently become the next recording's setting. That reason disappears the moment the
choice applies now — and only then.

### Step 8 — Tests, and the parts only a person can check

Python: the stand-in helper in `tests/test_record.py` learns to announce pieces and
switches; merge tests with deliberately mismatched formats; a refused switch leaves the
recording untouched. Swift has no test harness, so the preview app carries a scenario
where a swap happens mid-recording and the captions change under it. The rest is a person
at a Mac: start on the built-in microphone, unplug the AirPods, watch the fallback.

### Step 9 — Write down what changed

The roadmap item is ticked; CLAUDE.md's paragraph on recording gains the piece and command
protocol; this file gains the protocol table as built rather than as planned.

## What could go wrong

- **A gap in the tape at the swap.** Step 0 measures it. Target under a quarter second,
  announced in the protocol, and never papered over by the merge — a recording that
  quietly loses a second of speech is worse than one that says it did.
- **A device that changes format without a swap.** Some interfaces move sample rate on
  their own. Rollover handles it identically, provided there is a listener on the format
  property as well as on the device list.
- **Two swaps in a moment.** Serialise commands on one queue and refuse a second while one
  is in flight, rather than interleaving two teardowns.
- **A swap to the device already recording.** Answer `switched` and touch nothing.

## Done means

A meeting can start on the built-in microphone, move to AirPods at minute three and back
at minute twenty, and the finished memo holds one continuous transcript of both speakers,
with no gap longer than a quarter second and no moment at which the menu bar claimed a
device that was not the one being recorded.
