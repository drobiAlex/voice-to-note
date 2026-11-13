# Roadmap

What vtn can do today, and what has been planned but not built.

## Next

- [ ] **Voice Memos watcher** — a phone recording syncs itself to `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`; a launchd + fswatch watcher copies each new file out (the folder itself is iCloud's — writing into it desyncs) and feeds it to `vtn process`, titled from the CloudRecordings.db beside it
- [ ] **To-dos on the phone via Apple Reminders** — two-way: a Reminders list per project (iCloud carries it to the iPhone), checking off in vtn completes the reminder and completing it on the phone marks the to-do done, last writer wins by modification time; goes through an EventKit helper beside `capture.swift`, not AppleScript — EventKit reads all 4088 reminders here in under a second, AppleScript takes 25 s to list eight lists. Apple Notes checklists are out: no scripting interface can read or write a checkbox
- [ ] **Insights on the phone via Apple Notes** — publish each memo's notes (summary, insights, decisions, open questions, dates, tags) as one note in a `vtn` folder, republished whenever the note changes, with each to-do line linking to its reminder so a tap opens the box to tick; a note edited on the phone is never overwritten
- [ ] **Chunk rollover in the recorder** — have `capture.swift` close and reopen its files on a cadence and announce each finished pair on the stdout protocol it already speaks, so live transcription reads whole files instead of tracking byte cursors through ones still being written
- [ ] **Change device mid-meeting** — the device pickers go quiet the moment a tape starts, because a choice made then would silently be a choice for the *next* recording; so a headset that dies twenty minutes into a call has no cure but stopping and starting again, which costs the meeting. The swap rides on chunk rollover above: `capture.swift` learns a command channel on its stdin, closes the pair of files it is writing, reopens them on the new device and announces the finished pair the way rollover already announces one — so a sample rate or a channel count that changes with the device changes a file boundary instead of corrupting a header written minutes ago. The merge concatenates the pieces and resamples where they differ, and live transcription needs nothing new, since it already reads whole finished files. It is the other half of the silence warning: amber says the microphone has gone dead, and this is what somebody does about it without losing the room. Step by step, with the why for each step, in `docs/device-swap-plan.md`
- [ ] **Two-speed transcript** — a small model keeping up with the room while the meeting runs, then one pass with the full model over the archive afterwards that replaces the words; immediate text and best-effort quality from the same recording, which is what makes a heavy model usable live on a machine without a GPU
- [ ] **Background worker and job queue** — durable processing that survives restarts, batch imports, and the shape a future always-on server needs; deliberately deferred until that server becomes real
- [ ] **Full-text search** — across transcripts and notes

## Shipped

### Capture

- [x] Record a meeting's system audio and microphone with `vtn record` or the menu bar app
- [x] Pick the project and the audio devices for a recording (`vtn devices`, menu bar pickers)
- [x] See live level meters and a silence hint while recording (`--levels` for scripts)
- [x] Transcribe a meeting while it records, so stopping leaves only the last stretch to do
- [x] Import a YouTube video's captions as a memo with `vtn youtube`

### Transcription

- [x] Local pipeline: transcript, named speakers and structured notes (whisper.cpp, sherpa-onnx, SQLite)
- [x] Name a voice once and later memos recognise the speaker
- [x] Repair transcription errors with `vtn refine`
- [x] Add a recording from the TUI, choosing speaker count, template and steps per recording

### Notes and to-dos

- [x] Edit extracted notes, and write your own note templates beside the built-ins
- [x] Extract notes with claude, codex, gemini or ollama, tried in the order you set
- [x] Track to-dos per memo and check them off from the board, beside a note or with `vtn todo`
- [x] Ask a question about a memo with `vtn ask`
- [x] Keep a conversation about one or more memos with `vtn chat`, and reopen it with `vtn chats` or in the TUI

### Library

- [x] Organise memos with projects and tags; rename, move and delete them from the list or the command line
- [x] See every memo at once in the TUI, sorted by creation or last update
- [x] Archive a memo instead of deleting it (`vtn archive`, `vtn list --archived`)

### Agents and scripting

- [x] Read results from the command line, with `--json` on `list`, `show` and `notes`
- [x] See how long each stage takes and how runs end with `vtn stats`
- [x] Write one file to send when something misbehaves with `vtn report`

### Setup and interface

- [x] Install in one command, with a mock preview mode
- [x] Browse, edit and process memos in the TUI with live job progress
- [x] Change any setting in `vtn.toml`, from the TUI settings screen or with `vtn config`
