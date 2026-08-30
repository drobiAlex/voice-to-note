# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv run pytest -q                  # 955 tests, spread over the cores by default
uv run pytest -m "not ui"         # skip the Textual Pilot tests (tests/test_tui.py)
uv run pytest tests/test_services.py::test_name   # single test
uv run mypy src
uv run ruff check src tests
```

Two ways to actually run the app, against two different `VTN_HOME`s:

```sh
vtn tui                           # the installed uv tool — real memos, real models
vtn process ~/memos/standup.m4a   # $HOME/Library/Application Support/vtn
uv tool install . --reinstall     # put working-tree changes behind that `vtn`

./run.sh                          # dev entry: VTN_HOME=checkout, uv sync, vtn setup
./run.sh process path/to/memo.m4a # same subcommands, checkout-local db and models
```

`vtn` is the day-to-day command and the one the user's own memos live behind; the installer
(`install.sh`) puts it there with `uv tool install git+<repo> --reinstall`, which is also the
upgrade path. `./run.sh` exists so a change can be exercised without touching that database —
it exports `VTN_HOME=$PWD`, so `data/`, `models/`, `vendor/` and `vtn.toml` land in the checkout
(all gitignored). Editing source does **not** change the installed `vtn` until it is reinstalled.

CI (ubuntu) runs ruff → mypy → `pytest -q --cov=voice_to_note --cov-fail-under=84`. Tests must
stay runnable off macOS even though the app itself is macOS-only.

`VTN_INSTALL_MOCK=1 ./install.sh` and `vtn setup --mock` preview the whole install in seconds
without touching git, cmake or the network.

## Architecture

Four layers, strictly one-directional. `services.py` is the only module that composes the others.

- **`cli.py` / `tui/app.py`** — two front ends over the same use cases. Neither is imported by
  `services`. `cmd_tui` imports Textual lazily; importing it at module top would cost every other
  command as much as the rest of the app.
- **`services.py`** — every use case (`process_memo`, `run_extraction`, `refine_transcript`,
  `setup`, config/template management, row formatting for both front ends). Raises `NotFound`,
  `ExtractionError`, `InvalidInput`.
- **`gateways/`** — everything outside the process, each raising `GatewayError`: `audio` (ffmpeg),
  `whisper` (whisper-cli subprocess), `sherpa` (onnx diarization in a spawned process pool), `llm`
  (claude/codex/gemini CLIs, ollama HTTP), `bootstrap` (clone/build/download during setup),
  `capture` (native Swift recorder), `qos` (taskpolicy/nice wrapping).
- **`transforms/`** — pure functions, no I/O: `segments`, `speakers`, `notes`, `refine`, `todos`,
  `live` (where to cut a chunk of a meeting, and how its lines fit into the whole).
- **`storage/repository.py`** — every SQL statement in the app. `domain.py` holds the frozen
  dataclasses and TypedDicts both sides speak in.

`cli.main` catches exactly `NotFound | ExtractionError | InvalidInput | GatewayError` and exits with
one line; everything else keeps its traceback on purpose, because that is the only thing that
locates a bug.

Output contract, relied on by tests and by agents driving the CLI: results on stdout, progress and
confirmations on stderr (`cli.status`), `--json` on `list`/`show`/`notes`.

## Things that will bite

**Config resolves at import.** `config.py` reads `VTN_<KEY>` env → `$VTN_HOME/vtn.toml` → default
into module-level constants when the module is first imported. Setting an env var later changes
nothing; tests monkeypatch `config.CLAUDE_MODEL` and friends directly. `VTN_HOME` decides where the
DB, models, vendored whisper.cpp, templates and native binaries all live.

**Settings are a registry.** A new knob is one `Setting(default, cast, doc, kind=, choices=)` entry
in `config.SETTINGS` plus the module constant. `vtn config`, the TUI settings screen and its
per-kind editor all read the registry — none of them need touching.

**LLM backends are a chain, not a choice.** `config.llm_backends` names them in order;
`services._complete` tries each available one and demotes a backend whose reply will not parse.
Entries in `llm.BACKENDS` call the module-level functions by bare name at call time so
monkeypatching `llm.claude_complete` still reaches them. Extraction runs through the CLIs the user
already signed in to — no API keys anywhere.

**Prompt templates.** `llm.TEMPLATES` ships the built-ins; `$VTN_HOME/templates/<name>.md`
overrides one, re-read on every call. An override may reword the ask but not the JSON shape, which
the parser downstream enforces.

**Migrations are additive.** `Repository._migrate` guards each `ALTER TABLE` with
`PRAGMA table_info`; there is no version table. A brand-new `todos` table triggers a one-off
backfill from stored extractions.

**To-dos reconcile by normalized text.** Re-extraction matches fresh action items to stored rows via
`transforms.todos.normalize`; the `touched` flag keeps a row somebody has checked off or edited from
being dropped when the model rewords it.

**A meeting is transcribed while it records.** `vtn record` opens a memo at `status='recording'`
and `services.LiveSession` appends segments to it a stretch at a time, reading the two wavs the
helper is still writing — `capture.TrackReader` goes by the file's size on disk, never by the RIFF
header, which AVAudioFile writes short until close. Where a stretch is cut is the whole ballgame:
`transforms.live.cut_offset` puts the cut in a pause, and cutting blind instead costs 160% more
decoding time (`bench/` measured it on 25 minutes of speech). The session runs on its own thread
with its own connection (`open_repo`), never more than one transcription at a time, and takes
longer stretches rather than starting a second pass when it falls behind. Nothing about it may
fail a recording: an unreadable track, a missing model or `live_transcribe=off` all just leave
`vtn record` transcribing the merged file the ordinary way.

**Diarization spawns a process.** `sherpa._spawn_safe_stderr` exists because a full-screen TUI
replaces `sys.stderr` with a capture object whose `fileno()` is -1, which `multiprocessing` refuses.

**sherpa-onnx comes from the k2-fsa index.** PyPI's macOS wheel omits the bundled onnxruntime dylib;
`[tool.uv.sources]` in pyproject pins it, and the installer verifies the pin held.

## House style

- Every function carries a prose docstring saying *why* — the invariant, the tradeoff, the failure
  mode it prevents. Never changelog phrasing, never ticket references.
- Test names are full sentences describing behavior
  (`test_checking_a_task_off_ticks_its_box_in_the_note_behind_the_board`).
- Ruff selects `E4,E7,E9,F,B,I,UP` only — there is no line-length rule, so the aligned multi-argument
  subprocess calls in the gateways are intentional, not oversights.
- mypy runs over `src` only, with `disallow_untyped_defs`; tests are unchecked.
- Commits: conventional prefix plus a lowercase human sentence
  (`feat: point a recording at chosen audio devices`), body only when the why is not obvious.
