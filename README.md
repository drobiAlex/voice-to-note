# Voice to note

You sit in meetings across a lot of projects, and each one ends with its own decisions
and its own things to do. voice-to-note gives them one home: record a voice memo, feed it
in, and get back a transcript with named speakers, a summary, action items and decisions.

Your audio never leaves the machine: ffmpeg normalises it, whisper.cpp transcribes, and
sherpa-onnx separates the speakers, all into a local SQLite file. Only the notes need an
LLM — `claude` if you have it, or a local Ollama model; the transcript needs neither.

## Quickstart

```sh
./run.sh                            # checks tools, builds whisper.cpp, fetches models
./run.sh process ~/memos/standup.m4a
```

`run.sh` is idempotent and forwards its arguments to `vtn` once setup is done.

| Command | What it does |
|---|---|
| `vtn process <file>` | convert, transcribe, diarize, store, extract notes |
| `vtn list` | list stored memos |
| `vtn show <id>` | print a memo's transcript |
| `vtn notes <id>` | print the extracted notes |
| `vtn extract <id>` | (re)run note extraction; `--force` replaces notes you edited |
| `vtn diarize <id>` | (re)run diarization on a stored memo |
| `vtn refine <id>` | repair transcription errors; `--diff` shows them without storing |
| `vtn ask <id> <question…>` | ask a question about one memo |
| `vtn info <id>` | what state a memo is in, and when it last changed |
| `vtn rename <id> <label> <name>` | name a speaker; later memos match by voice |
| `vtn move <id> <project>` | file a memo under another project |
| `vtn project rename <old> <new>` | rename a project, carrying every memo in it |
| `vtn project remove <name>` | empty a project; its memos go back to `other` |
| `vtn tui` | browse, edit and process memos on one screen |

Once a memo has been refined, every reader shows the repaired wording; `vtn show
<id> --raw` prints the transcription as it was first heard.

Every memo belongs to a project — `--project work` when processing, `vtn move` after
the fact — and `vtn list --project work` narrows the list to one, as does `vtn list
--tag release` for a tag the notes carry. `vtn tui` opens the lot on a single screen,
where `e` edits a memo's notes as Markdown. The screen shows your wording from then on,
while `vtn notes` and `--json` keep printing the model's; `vtn notes --edited` prints
what the screen shows, and only `vtn extract --force` overwrites what you wrote.

Lower case acts on the memo you are reading, upper case on its project:
`o` add a recording · `e` edit notes · `i` details · `x` extract · `p` repair ·
`d` diarize · `a` ask · `m` move · `r` name a speaker · `t` raw transcript ·
`R` rename project · `X` empty project · `/` find by tag · `esc` back · `q` quit.

Progress goes to stderr and results to stdout, so redirecting a command captures
only its output. `list`, `show` and `notes` also take `--json`:

```sh
vtn notes 3 --json | jq .action_items
```

Settings come from `VTN_*` environment variables or an optional `vtn.toml` in the
project root — environment wins, then the file, then the default. See `config.py`
for the full set (`whisper_model`, `num_speakers`, `ollama_model`, and others).

## Architecture

Every module is defined by what it is allowed to touch. Arrows are the imports
each layer may make; anything not drawn here is not allowed.

```mermaid
flowchart TD
    cli["cli.py — parse args, print"] --> services["services.py — the operations"]
    services --> transforms["transforms/ — pure logic, no I/O"]
    services --> storage["storage/repository.py — every SQL statement"]
    services --> gateways["gateways/ — the outside world, no policy"]
    gateways -.->|wire formats only| transforms
    gateways --> ffmpeg[ffmpeg]
    gateways --> whisper[whisper.cpp]
    gateways --> sherpa[sherpa-onnx]
    gateways --> llm["claude CLI / ollama"]
```

`cli.py` never queries the database and never formats a transcript — services
does both, so there is one place each output is built. Every layer may use the
dataclasses in `domain.py`. Data lives in `data/`: the SQLite database, and
converted audio under `data/uploads/`.

A `GatewayError` means a setup problem the user can fix, such as a missing binary
or model, and ends the command with a single line of advice. Anything else is a
bug and keeps its traceback, which is the only thing that locates it.

## Development

```sh
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

CI runs all three on every push, with a coverage floor of 84%.
