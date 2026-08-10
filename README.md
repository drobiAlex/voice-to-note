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
| `vtn process <file>` | convert, transcribe, diarize, store, extract notes |
| `vtn list` | list stored memos |
| `vtn show <id>` | print a memo's transcript |
| `vtn notes <id>` | print the extracted notes |
| `vtn ask <id> <question…>` | ask a question about one memo |
| `vtn tui` | browse, edit and process memos on one screen |

`vtn --help` lists the full set (diarize, refine, projects, speaker naming, …).

## Roadmap

- Custom note templates — define your own structures for extracted notes
- Settings & configuration — choose the extraction model (Claude, Codex, and others)
- Native recording — record audio directly on your Mac instead of importing voice memos
- Full-text search across transcripts and notes

## Development

`./run.sh` is the development entry point — it keeps the database, models
and whisper.cpp build inside the checkout via `VTN_HOME`.

```sh
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

CI runs all three on every push, with a coverage floor of 84%.
