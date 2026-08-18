# Voice to note

voice-to-note records and analyses voice on your Mac — meetings, memos, calls —
and turns each recording into a transcript with named speakers, structured
notes, and a to-do list that survives regeneration. It ships an opinionated
note template — summary, action items, decisions — and takes custom templates
beside the built-ins.

- **Records natively** — a menu bar recorder (or `vtn record`) captures system
  audio and microphone together, Bluetooth headsets included, with project and
  device pickers; stop it and the memo transcribes, diarizes and lands in your
  archive on its own.
- **Privacy-first** — audio, transcription and speaker separation never leave
  the machine: ffmpeg converts, whisper.cpp transcribes, and sherpa-onnx
  separates speakers, all into a local SQLite file. Only note extraction uses
  an LLM, and you choose which one — the `claude` CLI, or a local Ollama
  model. With Ollama, nothing leaves the machine at all.
- **Knows who is talking** — name a voice once and later memos recognise it
  across recordings.
- **To-dos as first-class items** — action items become a per-project board
  (`T` in the TUI, `vtn todos` outside it); checking one off survives
  re-extraction, and the same commitment restated next week doesn't duplicate.
- **A memo archive, not a dump** — projects, tags, rename/move/delete like a
  file system, from the TUI or the CLI; the open TUI notices memos other
  processes store and refreshes itself.
- **Agent-ready** — humans get a TUI (`vtn tui`), agents get the same CLI:
  results go to stdout, progress to stderr, and `list`, `show` and `notes`
  take `--json`.

  ```sh
  vtn notes 3 --json | jq .action_items
  ```
- **Model choice** — the extraction model is configurable: the `claude` CLI
  or a local Ollama model today.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/drobiAlex/voice-to-note/main/install.sh | bash
```

This installs `uv` if needed, installs the `vtn` CLI, builds whisper.cpp
against Metal, and downloads the models. Needs macOS, the Xcode Command Line
Tools (for `git`), `cmake` and `ffmpeg`; the installer names whichever of
these is missing and how to get it.

To upgrade, re-run the installer.

## Usage

| Command | What it does |
|---|---|
| `vtn setup` | install whisper.cpp and all models (idempotent) |
| `vtn record` | record this Mac's meeting — system audio + mic — then process it; a live level meter per side shows a muted mic while it can still be fixed (`--levels` for the raw numbers); `--output-device`/`--input-device` pin devices |
| `vtn menubar` | open the menu bar recorder: one click to record, pickers for project and devices |
| `vtn process <file>` | convert, transcribe, diarize, store, extract notes — shape the run with --speakers, --steps, --template |
| `vtn list` | list stored memos |
| `vtn show <id>` | print a memo's transcript |
| `vtn notes <id>` | print the extracted notes |
| `vtn todos` | list open to-dos across memos; `vtn todo done <id>` checks one off |
| `vtn ask <id> <question…>` | ask a question about one memo |
| `vtn move <id> <project>` / `title` / `delete` | file, rename or throw away a memo |
| `vtn tui` | browse, edit and process memos on one screen |

Running `vtn` with no command opens the TUI too, once setup has run.

`vtn --help` lists the full set (diarize, refine, devices, projects, speaker
naming, templates, config, …).

## Roadmap

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

Later:
- [ ] Conversations about a memo — `vtn ask` answers one question and forgets it; keep the exchange as a stored thread so follow-ups ("and who agreed to that?") carry the earlier turns, and let the TUI browse past chats beside the note
- [ ] Live level meters in the recorder — a waveform strip per side (system audio and the microphone) in the menu bar app, so a muted mic or a silent system tap shows up while recording rather than an hour later in the transcript
- [ ] Voice Memos watcher — a phone recording syncs itself to `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`; a launchd + fswatch watcher copies each new file out (the folder itself is iCloud's — writing into it desyncs) and feeds it to `vtn process`, titled from the CloudRecordings.db beside it
- [ ] To-dos on the phone via Apple Reminders — two-way: a Reminders list per project (iCloud carries it to the iPhone), checking off in vtn completes the reminder and completing it on the phone marks the to-do done, last writer wins by modification time; goes through an EventKit helper beside `capture.swift`, not AppleScript — EventKit reads all 4088 reminders here in under a second, AppleScript takes 25 s to list eight lists. Apple Notes checklists are out: no scripting interface can read or write a checkbox
- [ ] Insights on the phone via Apple Notes — publish each memo's notes (summary, insights, decisions, open questions, dates, tags) as one note in a `vtn` folder, republished whenever the note changes, with each to-do line linking to its reminder so a tap opens the box to tick; a note edited on the phone is never overwritten
- [ ] Background worker & job queue — durable processing that survives restarts, batch imports, and the shape a future always-on server needs; deliberately deferred until that server becomes real
- [ ] Full-text search across transcripts and notes

## Development

`./run.sh` is the development entry point — it keeps the database, models
and whisper.cpp build inside the checkout via `VTN_HOME`.

```sh
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

CI runs all three on every push, with a coverage floor of 84%.
