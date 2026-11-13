# Roadmap

Shipped:
- [x] Local pipeline: transcript, named speakers, structured notes (whisper.cpp + sherpa-onnx + SQLite)
- [x] Projects, tags and notes you can edit
- [x] Speaker recognition across memos — name a voice once, later memos match it
- [x] Transcript repair pass (`vtn refine`)
- [x] Questions against a memo (`vtn ask`)
- [x] TUI with live job progress
- [x] One-command install, with a mock preview mode
- [x] Settings & configuration — every knob in vtn.toml, editable from the TUI settings screen or `vtn config`
- [x] Custom note templates — write your own beside the built-ins, pick one per recording
- [x] Add-recording form — speaker count, template and pipeline steps chosen per recording
- [x] Native meeting recording — system audio + microphone, from `vtn record` or the menu bar app
- [x] Menu bar recorder with project and device pickers — system mix by default, any output or microphone by choice
- [x] Memo management like a file system — delete, rename and move memos from the list or the command line
- [x] To-dos as first-class items — counted per memo, reconciled through re-extraction, checked off from the board (`T`), beside a note (`c`) or the command line
- [x] Live level meters in the recorder — a waveform strip per side beside the timer in the menu bar, native level indicators and a silence hint in its menu, a live meter in the terminal for `vtn record`, and `--levels` for anything else that wants the numbers
- [x] Every memo at once — an All row above the projects in the TUI sidebar, a project column in the table, and every listing sortable by creation or last update (`s` in the TUI, `vtn list --sort`)
- [x] Transcribing a meeting while it is still being recorded — `vtn record` reads the two growing tracks a stretch at a time, cuts each stretch where nobody is talking, and stores the words as they are said; stopping the recording leaves the last stretch and the speaker pass instead of the whole meeting. Measured over 25 minutes of speech: the wait after the last word drops from 204 s to 15 s, and the work costs the same — chunks cut at a pause were within 13% of the whole file, and at two minutes each were 10% cheaper than it, while chunks cut blind on the minute cost 160% more. `live_transcribe`, `live_chunk_s` and `live_model` in `vtn config`; a live pass that cannot run costs nothing but itself
- [x] Archiving instead of deleting — a memo you are done with goes in a drawer under the projects (`h` in the TUI, `vtn archive`/`unarchive`, `vtn list --archived`), taking its to-dos off the board with it; nothing is rewritten, so what comes back out is exactly what went in, and opening a memo by id still works while it is away

Later:
- [ ] Conversations about a memo — `vtn ask` answers one question and forgets it; keep the exchange as a stored thread so follow-ups ("and who agreed to that?") carry the earlier turns, and let the TUI browse past chats beside the note
- [ ] Voice Memos watcher — a phone recording syncs itself to `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`; a launchd + fswatch watcher copies each new file out (the folder itself is iCloud's — writing into it desyncs) and feeds it to `vtn process`, titled from the CloudRecordings.db beside it
- [ ] To-dos on the phone via Apple Reminders — two-way: a Reminders list per project (iCloud carries it to the iPhone), checking off in vtn completes the reminder and completing it on the phone marks the to-do done, last writer wins by modification time; goes through an EventKit helper beside `capture.swift`, not AppleScript — EventKit reads all 4088 reminders here in under a second, AppleScript takes 25 s to list eight lists. Apple Notes checklists are out: no scripting interface can read or write a checkbox
- [ ] Insights on the phone via Apple Notes — publish each memo's notes (summary, insights, decisions, open questions, dates, tags) as one note in a `vtn` folder, republished whenever the note changes, with each to-do line linking to its reminder so a tap opens the box to tick; a note edited on the phone is never overwritten
- [ ] Chunk rollover in the recorder — have `capture.swift` close and reopen its files on a cadence and announce each finished pair on the stdout protocol it already speaks, so live transcription reads whole files instead of tracking byte cursors through ones still being written
- [ ] Change the microphone or the sound source mid-meeting — the device pickers go quiet the moment a tape starts, because a choice made then would silently be a choice for the *next* recording; so a headset that dies twenty minutes into a call has no cure but stopping and starting again, which costs the meeting. The swap rides on chunk rollover above: `capture.swift` learns a command channel on its stdin, closes the pair of files it is writing, reopens them on the new device and announces the finished pair the way rollover already announces one — so a sample rate or a channel count that changes with the device changes a file boundary instead of corrupting a header written minutes ago. The merge concatenates the pieces and resamples where they differ, and live transcription needs nothing new, since it already reads whole finished files. It is the other half of the silence warning: amber says the microphone has gone dead, and this is what somebody does about it without losing the room. Step by step, with the why for each step, in `docs/device-swap-plan.md`
- [ ] A two-speed transcript — a small model keeping up with the room while the meeting runs, then one pass with the full model over the archive afterwards that replaces the words; immediate text and best-effort quality from the same recording, which is what makes a heavy model usable live on a machine without a GPU
- [ ] Background worker & job queue — durable processing that survives restarts, batch imports, and the shape a future always-on server needs; deliberately deferred until that server becomes real
- [ ] Full-text search across transcripts and notes
