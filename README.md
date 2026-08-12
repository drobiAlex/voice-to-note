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
| `vtn record` | record this Mac's meeting — system audio + mic — then process it; `--output-device`/`--input-device` pin devices |
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
- [ ] Voice Memos watcher — a phone recording syncs itself to `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`; a launchd + fswatch watcher copies each new file out (the folder itself is iCloud's — writing into it desyncs) and feeds it to `vtn process`, titled from the CloudRecordings.db beside it
- [ ] Background worker & job queue — durable processing that survives restarts, batch imports, and the shape a future always-on server needs; deliberately deferred until that server becomes real
- [ ] Full-text search across transcripts and notes

## Similar projects

Worth knowing about; each was closest to voice-to-note from a different angle
when we looked (2026). What keeps this project separate is the combination:
terminal-first with a real TUI, a project/tag memo archive, to-dos that
survive re-extraction, and extraction through the LLM CLIs you already pay
for — no API keys.

| Project | Closest when | Tradeoff |
|---|---|---|
| [Mila](https://github.com/island-io/mila) | iPhone Voice Memos / imported files are central: local whisper.cpp, diarization with persistent names, Voice Memos folder watching | macOS GUI app; less of a project/TUI workflow |
| [Meeting Transcriber](https://github.com/pasrom/meeting-transcriber) | very close pipeline: local transcription, diarization, Markdown protocol, Claude CLI / Ollama, file import | built around automatic live meeting capture rather than a memo archive |
| [Minutes](https://github.com/silverstein/minutes) | local meeting/voice-note memory, Markdown storage, CLI and AI-agent recall | deliberately broader — MCP, cross-agent memory, policy controls |
| [Muesli](https://github.com/Muesli-HQ/muesli) | mature native macOS option: import, diarization, local notes, folders, exports | a large product — dictation, calendar, hotkeys, many ASR engines, sync |
| [ownscribe](https://github.com/paberr/ownscribe) | closest small Python CLI: WhisperX, diarization, templates, Ollama, cross-meeting questions | heavier WhisperX/pyannote stack; centers live system-audio capture |

## Development

`./run.sh` is the development entry point — it keeps the database, models
and whisper.cpp build inside the checkout via `VTN_HOME`.

```sh
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

CI runs all three on every push, with a coverage floor of 84%.
