# voice-to-note

Turns a voice memo into structured notes, entirely on your machine:

```
audio → ffmpeg (16kHz mono) → whisper.cpp → sherpa-onnx diarization → LLM extraction → SQLite
```

Speakers are recognised across memos: name someone once with `vtn rename` and
their voice is matched automatically in later recordings.

## Setup

```sh
./run.sh
```

This checks for `uv`, `git`, `cmake` and `ffmpeg`, builds whisper.cpp, downloads
the whisper, VAD, segmentation and speaker-embedding models, and installs
dependencies. Run it once; afterwards `run.sh` forwards its arguments to `vtn`.

Note extraction uses the `claude` CLI when it is installed, and falls back to a
local Ollama model. Without either, transcription and diarization still work and
`vtn extract` can be run later.

## Commands

| Command | What it does |
|---|---|
| `vtn process <file>` | convert, transcribe, diarize, store, extract notes |
| `vtn list` | list stored memos |
| `vtn show <id>` | print a memo's transcript |
| `vtn notes <id>` | print the extracted notes |
| `vtn extract <id>` | (re)run note extraction |
| `vtn diarize <id>` | (re)run diarization on a stored memo |
| `vtn ask <id> <question…>` | ask a question about one memo |
| `vtn rename <id> <label> <name>` | name a speaker |

`vtn --version` prints the installed version and `vtn --help` lists every
command with usage examples.

```sh
vtn process ~/memos/standup.m4a
vtn list
vtn show 3
vtn rename 3 S1 Samantha        # later memos match Samantha by voice
vtn ask 3 what did we decide about pricing
vtn notes 3 > standup.md
```

Progress messages go to stderr and results go to stdout, so redirecting a
command captures only its output. `list`, `show` and `notes` also accept
`--json` for scripting:

```sh
vtn notes 3 --json | jq .action_items
vtn show 3 --json | jq -r '.[] | "\(.speaker): \(.text)"'
```

## Configuration

Every setting can come from an environment variable or from an optional
`vtn.toml` in the project root. Environment wins, then `vtn.toml`, then the
default.

| `vtn.toml` key | Environment variable | Default |
|---|---|---|
| `whisper_model` | `VTN_WHISPER_MODEL` | `large-v3-turbo` |
| `emb_model` | `VTN_EMB_MODEL` | `nemo_en_titanet_large.onnx` |
| `num_speakers` | `VTN_NUM_SPEAKERS` | `-1` (detect automatically) |
| `diar_threshold` | `VTN_DIAR_THRESHOLD` | `0.5` |
| `match_threshold` | `VTN_MATCH_THRESHOLD` | `0.5` |
| `claude_model` | `VTN_CLAUDE_MODEL` | `sonnet` |
| `ollama_url` | `VTN_OLLAMA_URL` | `http://localhost:11434` |
| `ollama_model` | `VTN_OLLAMA_MODEL` | `qwen3:8b` |

```toml
# vtn.toml
whisper_model = "medium"
num_speakers = 2
match_threshold = 0.6
```

## Architecture

The code is split by what a module is allowed to touch. `domain.py` holds the
dataclasses (`Segment`, `Turn`, `Speaker`, `Memo`, `Extraction`). `transforms/`
is pure logic with no I/O — whisper JSON to segments, speaker assignment and
label normalization, embedding maths, LLM output parsing, notes rendering — so
it is directly testable. `storage/repository.py` holds every SQL statement in
the app; nothing else touches the database. `gateways/` wraps the four external
systems (ffmpeg, whisper-cli, sherpa-onnx, the LLM backends) and contains no
policy. `services.py` composes those into the operations the app performs, and
`cli.py` only parses arguments and prints.

Data lives in `data/`: `voice_to_note.db` (memos, segments, speakers with voice
embeddings, extractions) and `data/uploads/` for converted audio.

## Tests

```sh
uv run pytest -q
uvx ruff@0.16.2 check src tests   # CI pins the same version
```

The suite covers business logic only — speaker attribution, label
normalization, transcript merging, LLM output parsing, voice matching,
repository round-trips and service orchestration — with no external processes
or models required.
