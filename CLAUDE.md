# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv run pytest -q                  # 1184 tests (4 more skipped), spread over the cores by default
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

The numbers the optimisation notes below argue from come from `bench/`, which needs its fixtures
built once (`bench/make_set.py`, then `bench/make_speech.py`; ~100 MB, gitignored):

```sh
VTN_HOME=$PWD .venv/bin/python bench/run.py       # every stage that needs no model, then the
                                                  # listing at 50/100/200 memos, indexed and not
VTN_HOME=$PWD .venv/bin/python bench/models.py    # whisper and diarization; needs setup to have run
VTN_HOME=$PWD .venv/bin/python bench/pipeline.py  # the real process_memo, both overlap modes
```

`VTN_HOME` is not optional there: `run.py` and `pipeline.py` delete `config.DB_PATH` before they
start, and unset that is the user's real memo library rather than a throwaway — both refuse to run
without it. A stage whose model is missing prints as `skipped`, never as fast.

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
  `capture` (native Swift recorder), `qos` (taskpolicy/nice wrapping), `youtube` (yt-dlp
  subprocess plus a caption GET — the words arrive as captions, never as audio), `eventlog`
  (the capped JSON-lines log under `$VTN_HOME/logs`; the one gateway that never raises).
- **`transforms/`** — pure functions, no I/O: `segments`, `speakers`, `notes`, `refine`, `todos`,
  `live` (where to cut a chunk of a meeting, and how its lines fit into the whole), `youtube`
  (which caption track to take, and how its events fold into timed paragraphs), `metrics`
  (medians, p90s and speeds over stored runs), `levels` (spotting a side that went silent while
  the other talked).
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
the parser downstream enforces. The subset a `--template` flag may pick lives in
`llm.NOTE_TEMPLATES` (notes/interview/lecture/tutorial/learning, all sharing `_NOTES_SHAPE`);
`services.note_templates()` filters anything registered in `TEMPLATES` out of the custom-file list,
so a new shipped note template goes into `NOTE_TEMPLATES` — putting it only in `TEMPLATES` hides
it (and into `services._TEMPLATE_DOCS`). `vtn template new <name> --from <existing>` copies one
into a custom file, front matter included.

A template may open with TOML front matter between two `+++` lines; `transforms.templates.
parse_template` splits it into a frozen `ParsedTemplate(body, sections, questions, tables)`. `[sections]`
(heading = what goes there, in file order) makes vtn itself add the instruction for an optional
`sections` key to the prompt, ahead of the trailing `Transcript:` line — the author never edits the
JSON shape. Other tables are kept in `tables`, not rejected. `parse_notes` keeps well-formed section
entries and drops the rest; both renderers show them after the fixed sections. Broken front matter
raises `InvalidInput` naming the file (`llm.parsed_template` adds the path, `services.note_template`
converts it, so every `--template` check catches it early).

`[ask]` (key = question, key `[a-z0-9_]+`, file order) lets a template ask the person things before
it runs; every answer is optional. `compose_prompt(template, transcript, answers, details)` fills
exact `{key}` tokens in the template body — never `str.format`, the JSON shape is full of braces —
for the ask keys and the built-in details `{title} {channel} {url} {recorded} {project}`; a known
name with no value reads `(not given)`. Before the trailing `Transcript:` it adds, in order, the
sections instruction, an "About this recording" block (a video: title, channel, link, upload day;
anything else: only the recorded day; never the project, which on a re-extraction is the memo's own earlier suggestion) and a block of the answered
questions. Answers live in `extractions.answers` (JSON); `services.extraction_answers` lays `--var`
or form answers over the stored ones and keeps only keys the template asks, so switching templates
drops the rest. `run_extraction(..., *, answers=)`; the CLI takes repeatable `--var key=value` and
never prompts, the TUI asks through `TemplateQuestions`, which returns its answers as the screen's
result. `memos.channel` is what the video details read.

Each source has a default template — settings `youtube_template` (`video`), `record_template` and
`file_template` (`notes`) — resolved in `services.default_template(source)` / `resolve_template`.
They are free text because custom names are only known at run time, so they are validated at use,
and a stale name raises an error that names the setting. `--template` is None unless given. The
`extractions.template` column remembers the name; `run_extraction` without one uses
`extraction_template` (stored name if it still exists, else the source default — a memo with
`source_url` is a video, anything else counts as a file since a recording leaves no mark).

**A memo can come from a video.** `vtn youtube <url>` stores YouTube captions as ordinary timed
segments with `wav_path=''` and no speakers — status lands `'transcribed'`, so extract/refine/
ask/chat all just work. Duplicates are recognised by the canonical `source_url` column, not
`recorded_at` (the upload day only stamps midnight). Which caption track is taken is `transforms.youtube.pick_track` alone
(written, then auto-generated, then — only into a language that was asked for — YouTube's machine
translation, which yt-dlp lists in `automatic_captions` with `tlang=` in the URL); `describe_captions`
asks it rather than repeating its rules. `youtube_lang` is an ordered comma-separated preference of
names or codes (`transforms.languages` resolves them); the TUI form's language Select is refilled from
`services.caption_options` after an off-thread lookup, and the looked-up video rides in
`YouTubeOptions.video` so the import does not fetch twice. yt-dlp is a subprocess on purpose: the user
keeps it current with brew as YouTube churns, which is also the first fix for any fetch failure.

**Delete means the bin.** `memos.deleted_at` set means the memo is in the bin: every row under it and the wav are untouched, and `services.delete_memo` does nothing else. A binned memo must be absent from every listing and count — list, archive drawer, projects, tags, the to-do board, chat listings and scopes, and the voice pool later recordings are matched against — so a new query that lists memos, or joins from a child table to them, filters `deleted_at IS NULL` or it resurrects what somebody deleted. `deleted_at` is independent of `archived_at`, which is preserved through bin and restore. `services.require_memo` raises `InBin` (a `NotFound`: `memo 12 is in the bin — vtn restore 12`) for every by-id use case; only restore, `purge_memo` and `info` pass `binned_ok`. Permanent removal is `purge_memo` / `empty_bin` only, behind a confirmation in the front end that purges only the ids it showed and only while they are still binned (`empty_bin(shown=…)`, `purge_memo(only_if_binned=True)`); a recording is never binned or purged, and nothing purges on its own — no age, timer or startup sweep. In the TUI the memo keys act on the highlighted row (`_target_memo`), never on the memo left open below the list.

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

**Everything slow is timed, and both records are capped.** `services.tracked(repo, kind)` opens
a run, `services.stage(name)` times a step of whichever run is current (a `ContextVar`, so a
worker thread's stages land in the event log only), and both write to the `runs`/`run_stages`
tables and to `gateways/eventlog`. The tables keep `metrics_keep_runs` runs, pruned on insert; the
log is three rotating files sharing `log_max_mb`. A timing that cannot be written costs the timing,
never the work. `vtn stats` reads the tables; `vtn report` bundles them with the log tails and
nothing anybody said. Tests send events to a tmp folder through an autouse fixture in
`conftest.py` — without it a suite run would write into the developer's real log.

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

## Swift on the remote Mac

Puck motion is configured in `PuckMotion` in `native/menubar.swift`; window physics and
Core Animation springs derive from the same constants. `native/springs.swift` is vendored
CocoaSprings physics: preserve its upstream formatting and headers, and exempt it from the
prose-docstring rule so future upstream comparisons remain meaningful.

`capture.swift` / `menubar.swift` only compile on macOS. `scripts/remote-build.sh check|build|test|verify`
pushes the committed branch to the Mac and runs it there through a pinned SSH key that
cannot open a shell — `docs/remote-build.md` says what is allowed, what is never done
(launching the recorder, touching the installed `vtn`), and how the agent is expected to use it.
