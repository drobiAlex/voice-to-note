# Voice to note

voice-to-note is a simple, small framework for analysing voice recordings and
extracting useful information from them. Today it ships one opinionated note
template — summary, action items, decisions, and named speakers — with more
templates for custom note structures on the way.

- **Privacy-first** — audio, transcription and speaker separation never leave
  the machine: ffmpeg converts, whisper.cpp transcribes, and sherpa-onnx
  separates speakers, all into a local SQLite file. Only note extraction uses
  an LLM, and you choose which one — the `claude` CLI, or a local Ollama
  model. With Ollama, nothing leaves the machine at all.
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
| `vtn process <file>` | convert, transcribe, diarize, store, extract notes — shape the run with --speakers, --steps, --template |
| `vtn list` | list stored memos |
| `vtn show <id>` | print a memo's transcript |
| `vtn notes <id>` | print the extracted notes |
| `vtn ask <id> <question…>` | ask a question about one memo |
| `vtn tui` | browse, edit and process memos on one screen |

Running `vtn` with no command opens the TUI too, once setup has run.

`vtn --help` lists the full set (diarize, refine, projects, speaker naming, …).

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
