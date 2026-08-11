import json
import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from . import config
from .domain import Extraction, Memo, Speaker, SpeakerMatch, Turn
from .gateways import GatewayError, audio, bootstrap, llm, sherpa, whisper
from .storage.repository import Repository
from .transforms.notes import SCHEMA, parse_notes, render_notes, render_notes_markdown
from .transforms.refine import (
    REFINE_SCHEMA,
    Chunk,
    accept_repairs,
    chunk_segments,
    merge_refinements,
    parse_refinements,
)
from .transforms.segments import (
    display_name,
    display_text,
    fmt_ts,
    segments_as_dicts,
    segments_from_whisper,
    transcript_text,
)
from .transforms.speakers import (
    assign_speakers,
    auto_named,
    match_known_speakers,
    resolve_speaker_names,
)

Log = Callable[[str], None]
# how far through the pipeline a recording has got: which stage is starting, and
# the word for it. A line of text says the same thing, but only a number can be
# drawn as a bar that fills
Progress = Callable[[int, str], None]
T = TypeVar("T")


class NotFound(Exception):
    """A memo, speaker or extraction the caller asked for does not exist."""


class ExtractionError(Exception):
    """No LLM backend produced a usable result."""


class InvalidInput(Exception):
    """Something the user typed cannot be used as given. Theirs to correct, so
    it earns one line of explanation rather than a traceback."""


@dataclass
class ProcessResult:
    """What `vtn process` reports back once a memo is safely stored."""

    memo_id: int
    segment_count: int
    labels: list[str]
    language: str


def _silent(message: str) -> None:
    """Default for callers that want the work done without progress reports."""
    pass


def _uncounted(stage: int, doing: str) -> None:
    """Default for callers with nowhere to draw how far the pipeline has got."""
    pass


def require_memo(repo: Repository, memo_id: int) -> Memo:
    """Finds a memo, or fails with something the user can act on."""
    memo = repo.memo(memo_id)
    if not memo:
        raise NotFound(f"no memo with id {memo_id}")
    return memo


def _project_name(project: str) -> str:
    """A project name with its spacing tidied, refusing one that is really empty:
    a nameless project would show as a blank column and a blank sidebar row that
    nobody could search for or move a memo out of."""
    name = project.strip()
    if not name:
        raise InvalidInput("a project needs a name")
    return name


def open_repo(repo: Repository) -> Repository:
    """A second connection to the same database, for work happening off the main
    thread. Sqlite refuses a connection used from a thread that did not open it,
    so a background job cannot borrow the one a screen is reading through; the
    database is in WAL mode, so the two do not block each other."""
    return Repository(repo.path)


def _tag(tag: str | None) -> str | None:
    """A tag with its spacing tidied, refusing one that is really empty: a blank
    tag matches nothing, and a search that was never really asked should not
    read the same as one that ran and honestly found nothing."""
    if tag is None:
        return None
    text = tag.strip()
    if not text:
        raise InvalidInput("a tag needs some text")
    return text


def _built(path: Path) -> bool:
    """Whether a binary this app shells out to is actually runnable, not just
    present: a file left over from an interrupted build would exist but not
    execute, and existence alone would then read as done."""
    return path.exists() and os.access(path, os.X_OK)


def _download_line(name: str, read: int, total: int) -> str:
    """One line of a download's progress, sized the way a person reads a
    file's weight rather than in raw bytes: megabytes to one decimal, with a
    percentage once the server has said how big the whole thing is."""
    mb = read / 1_000_000
    if not total:
        return f"  {name} {mb:.1f} MB"
    pct = int(read / total * 100)
    return f"  {name} {mb:.1f}/{total / 1_000_000:.1f} MB ({pct}%)"


def _report_step(
    log: Log, now: Callable[[], float], n: int, doing: str, work: Callable[[], None]
) -> None:
    """Times one setup step that actually has to run, saying what it is doing
    before the work and how long it took once it is done: the two lines a
    person staring at a silent terminal needs to know it has not hung."""
    log(f"[{n}/6] {doing} …")
    t0 = now()
    work()
    log(f"      done in {int(now() - t0)}s")


@dataclass(frozen=True)
class World:
    """Everything setup touches outside its own logic, swappable as one piece
    so a caller can preview the whole flow against a fake instead of git,
    cmake and the network."""

    require_tools: Callable[[], None]
    mkdir: Callable[[Path], None]
    exists: Callable[[Path], bool]
    built: Callable[[Path], bool]
    clone: Callable[[Path, str], None]
    build: Callable[[Path], None]
    script: Callable[[Path, str, list[str]], None]
    fetch: Callable[[str, Path, Callable[[int, int], None]], None]
    fetch_tar: Callable[[str, Path, Callable[[int, int], None]], None]


def _real_world() -> World:
    """The outside world setup actually touches. Read fresh on every call, so
    a test that monkeypatches bootstrap's own functions still reaches them."""
    return World(
        require_tools=bootstrap.require_tools,
        mkdir=lambda p: p.mkdir(parents=True, exist_ok=True),
        exists=lambda p: p.exists(),
        built=_built,
        clone=bootstrap.clone_whisper,
        build=bootstrap.build_whisper,
        script=bootstrap.download_model_script,
        fetch=bootstrap.fetch,
        fetch_tar=bootstrap.fetch_tar_bz2,
    )


# a plausible size for a mocked download's progress bar, keyed by the real
# artifact's filename so the preview looks like the run it stands in for
_MOCK_ARTIFACT_SIZES = {
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2": 6_000_000,
    "nemo_en_titanet_large.onnx": 97_000_000,
}


def _mock_artifact_size(url: str) -> int:
    """The byte count a mock download reports for a URL, taken from the real
    artifact's known size where it is one, or a plain guess otherwise."""
    return _MOCK_ARTIFACT_SIZES.get(Path(url).name, 50_000_000)


def mock_world(sleep: Callable[[float], None] = time.sleep) -> World:
    """A World that performs no real installation, for previewing the whole
    setup flow in seconds: every step "runs" against a stopwatch instead of
    git, cmake or the network, and nothing lands on disk. require_tools is
    left real since checking for git, cmake and ffmpeg costs nothing and a
    preview should still be honest about a machine that could not run this
    for real."""

    def clone(_vendor: Path, _repo_url: str) -> None:
        sleep(0.8)

    def build(_vendor: Path) -> None:
        sleep(2.0)

    def script(_vendor: Path, _name: str, _args: list[str]) -> None:
        sleep(1.5)

    def simulated_fetch(url: str, _dst: Path, tick: Callable[[int, int], None]) -> None:
        total = _mock_artifact_size(url)
        for step in range(1, 11):
            sleep(0.08)
            tick(total * step // 10, total)

    return World(
        require_tools=bootstrap.require_tools,
        mkdir=lambda _p: None,
        exists=lambda _p: False,
        built=lambda _p: False,
        clone=clone,
        build=build,
        script=script,
        fetch=simulated_fetch,
        fetch_tar=simulated_fetch,
    )


def setup(
    log: Log = _silent,
    download: Log = _silent,
    now: Callable[[], float] = time.monotonic,
    world: World | None = None,
) -> str:
    """Installs whisper.cpp and every model this app runs on, skipping any step
    whose artifact is already on disk so that re-running after a partial
    install only does what is still missing. A failed VAD download is the one
    step tolerated rather than raised: transcription still works without it,
    just without the pause-aware segmentation VAD gives it. A World stands in
    for every step that touches git, cmake or the network; passing mock_world()
    previews the same flow without doing any of it."""
    world = world or _real_world()
    world.require_tools()
    for d in (config.MODELS_DIR, config.DATA_DIR, config.UPLOADS_DIR):
        world.mkdir(d)

    if world.exists(config.VENDOR):
        log("[1/6] whisper.cpp source — already installed")
    else:
        _report_step(
            log,
            now,
            1,
            "cloning whisper.cpp",
            lambda: world.clone(config.VENDOR, config.WHISPER_REPO_URL),
        )

    if world.built(config.WHISPER_BIN):
        log("[2/6] whisper.cpp build — already installed")
    else:
        _report_step(
            log,
            now,
            2,
            "building whisper.cpp (Metal — takes a few minutes)",
            lambda: world.build(config.VENDOR),
        )

    if world.exists(config.WHISPER_MODEL_PATH):
        log("[3/6] whisper model — already installed")
    else:
        _report_step(
            log,
            now,
            3,
            f"downloading whisper model {config.WHISPER_MODEL}",
            lambda: world.script(
                config.VENDOR,
                "download-ggml-model.sh",
                [config.WHISPER_MODEL, str(config.MODELS_DIR)],
            ),
        )

    if world.exists(config.VAD_MODEL_PATH):
        log("[4/6] VAD model — already installed")
    else:
        log("[4/6] downloading VAD model …")
        t0 = now()
        try:
            world.script(
                config.VENDOR, "download-vad-model.sh", ["silero-v5.1.2", str(config.MODELS_DIR)]
            )
            log(f"      done in {int(now() - t0)}s")
        except GatewayError:
            log("VAD download failed — continuing without VAD")

    if world.exists(config.SEG_MODEL_PATH):
        log("[5/6] speaker segmentation model — already installed")
    else:
        _report_step(
            log,
            now,
            5,
            "downloading speaker segmentation model (~6 MB)",
            lambda: world.fetch_tar(
                "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
                config.MODELS_DIR,
                lambda read, total: download(
                    _download_line("sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", read, total)
                ),
            ),
        )

    # provisioned under its own fixed name, not config.EMB_MODEL_PATH: that
    # setting lets a user point at a model they supply themselves, and this
    # download must never land under a name it does not own
    default_emb_model = config.MODELS_DIR / "nemo_en_titanet_large.onnx"
    if world.exists(default_emb_model):
        log("[6/6] speaker embedding model — already installed")
    else:
        _report_step(
            log,
            now,
            6,
            "downloading speaker embedding model (~97 MB)",
            lambda: world.fetch(
                "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                "speaker-recongition-models/nemo_en_titanet_large.onnx",
                default_emb_model,
                lambda read, total: download(
                    _download_line("nemo_en_titanet_large.onnx", read, total)
                ),
            ),
        )

    return "setup complete"


def ready() -> bool:
    """Whether the pipeline has everything it needs to run: every artifact setup
    installs is on disk, save VAD, which transcription runs fine without."""
    return (
        _built(config.WHISPER_BIN)
        and config.WHISPER_MODEL_PATH.exists()
        and config.SEG_MODEL_PATH.exists()
        and config.EMB_MODEL_PATH.exists()
    )


def config_rows() -> list[tuple[str, str, str, str]]:
    """Every setting `vtn config` lists, in the order the registry defines
    them: its key, the value actually in effect on this machine, where that
    value came from, and what it does."""
    return [
        (
            key,
            str(config._setting(key)),
            config.source(key, os.environ, config._SETTINGS),
            setting.doc,
        )
        for key, setting in config.SETTINGS.items()
    ]


def _registered_setting(key: str) -> config.Setting:
    """The registry entry for a key a user typed, refusing one this app does
    not know about: `vtn config` lists every name it will accept."""
    setting = config.SETTINGS.get(key)
    if setting is None:
        raise InvalidInput(f"unknown setting: {key} (see: vtn config)")
    return setting


def setting_info(key: str) -> config.Setting:
    """One setting's registry entry — its default, kind and choices — for a
    screen building an editor around it, without reaching into config on its
    own. The key always comes from a listing config_rows() already produced,
    so it is always one this app knows about."""
    return _registered_setting(key)


def _env_override_note(key: str) -> str:
    """Told alongside a settings write when an environment variable means the
    value just written is not actually the one in effect: VTN_<KEY> always
    wins over vtn.toml, so a write under it would otherwise look like it did
    nothing."""
    if f"VTN_{key.upper()}" not in os.environ:
        return ""
    return f" — note: VTN_{key.upper()} is set in the environment and still wins"


def _write_confirmation(message: str, key: str) -> str:
    """A settings write's confirmation line: what changed, an environment
    override's warning if one applies, and the reminder that nothing takes
    hold until the next start — config is read once, at import time, so this
    process is still running on the value it started with."""
    return f"{message}{_env_override_note(key)} (takes effect on next start)"


def _toml_format(value: Any) -> str:
    """One value the way vtn.toml spells it: a quoted string, or a bare
    number — the shapes every registered setting's cast can produce."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_config_file(path: Path, settings: dict[str, Any]) -> None:
    """Rewrites vtn.toml from a settings dict, one `key = value` line per
    entry. Makes the app's own directory if setup has not run yet, since a
    settings write must not depend on that having already happened."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key} = {_toml_format(value)}" for key, value in settings.items()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def config_set(key: str, value: str) -> str:
    """Writes one setting into vtn.toml, cast the same way config.py reads it
    back so a value nothing downstream could use is refused before it ever
    reaches disk."""
    setting = _registered_setting(key)
    try:
        cast_value = setting.cast(value)
    except (ValueError, TypeError):
        raise InvalidInput(f"not a valid {key}: {value}") from None
    settings = config.read_config_file(config.CONFIG_PATH)
    settings[key] = cast_value
    _write_config_file(config.CONFIG_PATH, settings)
    return _write_confirmation(f"{key} set to {cast_value}", key)


def config_unset(key: str) -> str:
    """Drops one setting from vtn.toml, so it falls back to its default (or
    an environment override, if one is set). A key never written is left
    exactly as it was rather than rewriting a file to say the same thing."""
    _registered_setting(key)
    settings = config.read_config_file(config.CONFIG_PATH)
    if key not in settings:
        return f"{key} is already default{_env_override_note(key)}"
    del settings[key]
    _write_config_file(config.CONFIG_PATH, settings)
    return _write_confirmation(f"{key} unset", key)


def config_reset() -> str:
    """Clears vtn.toml of every setting this app registers, leaving any keys
    a user wrote by hand that it does not recognise: data this app does not
    own is not this reset's to delete."""
    settings = config.read_config_file(config.CONFIG_PATH)
    kept = {k: v for k, v in settings.items() if k not in config.SETTINGS}
    _write_config_file(config.CONFIG_PATH, kept)
    return "all settings reset to default (takes effect on next start)"


def _template_override_path(name: str) -> Path:
    """Where a saved override for one prompt template would live."""
    return config.TEMPLATES_DIR / f"{name}.md"


def _registered_template(name: str) -> None:
    """Refuses a template name this app does not ship: `vtn template` lists
    every name it will accept."""
    if name not in llm.TEMPLATES:
        raise InvalidInput(
            f"unknown template: {name} — valid names: {', '.join(llm.TEMPLATES)}"
        )


def template_rows() -> list[tuple[str, str]]:
    """Every prompt template this app ships, and whether a saved override is
    shadowing its built-in text."""
    return [
        (name, "overridden" if _template_override_path(name).exists() else "built-in")
        for name in llm.TEMPLATES
    ]


def template_text(name: str) -> str:
    """One template's effective text: a saved override if the user wrote one,
    else the text this app ships with."""
    _registered_template(name)
    return llm.template(name)


def template_reset(name: str) -> str:
    """Deletes a template's override file, restoring the built-in text. A
    template already at its built-in text is left alone rather than reporting
    a restore that did nothing."""
    _registered_template(name)
    path = _template_override_path(name)
    if not path.exists():
        return f"{name} is already built-in"
    path.unlink()
    return f"{name} restored to built-in"


def process_memo(
    repo: Repository,
    src: Path,
    project: str = "other",
    log: Log = _silent,
    progress: Progress = _uncounted,
) -> ProcessResult:
    """The whole pipeline for a new recording: audio in, stored memo out. Each
    stage is reported as it starts rather than as it ends, so that a screen
    counting them off is never a stage behind the work."""
    # before the minutes of converting and transcribing, not after
    project = _project_name(project)
    wav = config.UPLOADS_DIR / f"{src.stem}-{uuid.uuid4().hex[:8]}.wav"
    progress(1, "converting")
    log(f"converting {src.name} …")
    audio.to_wav16k(src, wav)
    duration = audio.duration_seconds(wav)
    progress(2, "transcribing")
    log(f"transcribing ({duration:.0f}s audio) …")
    raw = whisper.transcribe(wav, duration)
    segs = segments_from_whisper(raw)
    language = raw.get("result", {}).get("language", "")
    progress(3, "diarizing")
    log("diarizing …")
    turns = sherpa.diarize(wav)
    segs = assign_speakers(segs, turns)
    labels, speakers, matches = _identify(repo, wav, turns, keep_names={})
    memo_id = repo.create_memo(
        filename=src.name,
        wav_path=str(wav),
        duration_s=duration,
        language=language,
        segments=segs,
        speakers=speakers,
        project=project,
    )
    _report_matches(log, matches, keep_names={})
    return ProcessResult(memo_id, len(segs), labels, language)


def _speaker_count(num_speakers: int | None) -> int | None:
    """A speaker count pinned for re-diarization, refusing one that could not
    describe any recording: a memo always has at least one voice on it."""
    if num_speakers is not None and num_speakers < 1:
        raise InvalidInput(f"speaker count must be at least 1, got {num_speakers}")
    return num_speakers


def speaker_count(text: str) -> int | None:
    """A speaker count as typed into a modal before a redo of speaker
    detection: blank or "auto" leaves it to be guessed, anything else must
    actually be one. Public so a screen can refuse one while its modal is
    still open, rather than after it has closed on the typing."""
    typed = text.strip()
    if not typed or typed.lower() == "auto":
        return None
    try:
        count = int(typed)
    except ValueError:
        raise InvalidInput(f"not a speaker count: {typed}") from None
    return _speaker_count(count)


def rediarize(
    repo: Repository, memo_id: int, log: Log = _silent, num_speakers: int | None = None
) -> list[str]:
    """Runs speaker detection over a memo again, keeping the names people gave.
    A caller who knows how many voices are on the recording can pin that
    count instead of leaving it to be guessed."""
    num_speakers = _speaker_count(num_speakers)
    memo = require_memo(repo, memo_id)
    wav = Path(memo.wav_path)
    if not wav.exists():
        raise NotFound(f"wav missing: {wav}")
    keep_names = repo.named_speakers(memo_id)
    log(f"diarizing memo {memo_id} …")
    turns = sherpa.diarize(wav, num_speakers)
    segs = assign_speakers(repo.segments(memo_id), turns)
    labels, speakers, matches = _identify(repo, wav, turns, keep_names, exclude=memo_id)
    repo.save_diarization(memo_id, segs, speakers)
    _report_matches(log, matches, keep_names)
    return labels


def _identify(
    repo: Repository,
    wav: Path,
    turns: list[Turn],
    keep_names: dict[str, str],
    exclude: int | None = None,
) -> tuple[list[str], list[Speaker], dict[str, SpeakerMatch]]:
    """Works out who each voice belongs to, reusing names from earlier memos."""
    embeddings = sherpa.speaker_embeddings(wav, turns)
    pool = repo.known_embeddings(exclude_memo_id=exclude)
    matches = match_known_speakers(embeddings, pool, config.MATCH_THRESHOLD)
    labels = sorted({t.speaker for t in turns})
    return labels, resolve_speaker_names(labels, embeddings, matches, keep_names), matches


def _report_matches(
    log: Log, matches: dict[str, SpeakerMatch], keep_names: dict[str, str]
) -> None:
    """Tells the user which speakers were named by voice rather than by hand."""
    for label, m in auto_named(matches, keep_names):
        log(f"  {label} sounds like {m.name} (similarity {m.similarity:.2f}) — auto-named")


def transcript(repo: Repository, memo_id: int) -> str:
    """The speaker-labeled transcript a person or an LLM reads."""
    return transcript_text(repo.segments(memo_id), repo.display_names(memo_id))


def _duration(duration_s: float | None) -> str:
    """A recording's length in whole seconds, or a shrug when it was never
    measured. A zero-length recording reads as unmeasured too, which is right:
    nothing was captured either way."""
    return f"{duration_s:.0f}s" if duration_s else "?"


def memos_text(repo: Repository, project: str | None = None, tag: str | None = None) -> str:
    """The memo list as a person reads it, newest first and column-aligned so the
    ids, dates and states line up down the screen. One project or one tag at a
    time when asked for, and both together when both are. Empty when nothing is
    stored, and equally empty when nothing matches."""
    return "\n".join(
        f"{m.id:>4}  {m.created_at}  {_duration(m.duration_s):>6}  {m.language or '?':<3}"
        f"  {m.status:<12} {m.project:<8} {m.filename}"
        for m in memos(repo, project, tag)
    )


def memo_heading(repo: Repository, memo_id: int) -> str:
    """How a transcript is introduced on screen: which recording, and how far
    through the pipeline it got."""
    memo = require_memo(repo, memo_id)
    return f"memo {memo.id} — {memo.filename} ({memo.status})"


def transcript_lines(repo: Repository, memo_id: int, *, raw: bool = False) -> str:
    """The transcript as a person reads it: one timestamp-led line per segment,
    speakers under the names they were given. Shows each line's repair where one
    exists; raw shows the transcription instead. Empty when nothing was transcribed."""
    names = repo.display_names(memo_id)
    return "\n".join(
        f"{fmt_ts(s.t0_ms)}  {display_name(s.speaker, names)}: {display_text(s, raw=raw)}"
        for s in repo.segments(memo_id)
    )


def _backends() -> list[llm.Backend]:
    """The backends `_complete` tries, in the order config.llm_backends names
    them, refusing a name this app does not register: a typo in the setting
    must be caught here rather than silently trying fewer backends than the
    user asked for."""
    backends = []
    for name in config.LLM_BACKENDS.split(","):
        name = name.strip()
        if not name:
            continue
        backend = llm.BACKENDS.get(name)
        if backend is None:
            raise InvalidInput(f"unknown backend: {name} (see: vtn config)")
        backends.append(backend)
    return backends


def _complete(
    prompt: str,
    schema: dict | None,
    parse: Callable[[str], T],
    failure: str,
    unavailable: str,
    model: str | None = None,
) -> tuple[str, T]:
    """Gets an answer from the best backend that gives a usable one, tried in
    the order config.llm_backends names them; a reply that cannot be used
    demotes that backend rather than failing outright. The model steers
    whichever backends accept a per-call choice — others ignore it."""
    errors = []
    for backend in _backends():
        if not backend.available():
            continue
        try:
            return backend.describe(model), parse(backend.complete(prompt, schema, model))
        except (llm.BackendError, ValueError) as e:
            errors.append(f"{backend.name}: {e}")
    if errors:
        raise ExtractionError(f"{failure}: " + "; ".join(errors))
    raise ExtractionError(
        f"{unavailable}: install claude CLI, or run Ollama and"
        f" `ollama pull {config.OLLAMA_MODEL}`"
    )


def run_extraction(repo: Repository, memo_id: int, force: bool = False) -> str:
    """Turns a memo into structured notes and files them against it. A memo whose
    notes somebody has since edited by hand is left alone unless the caller says
    outright to overwrite: an afternoon of editing must not go under a rerun."""
    if not force and repo.notes_md(memo_id):
        raise InvalidInput(
            f"memo {memo_id} has notes you edited by hand;"
            " re-extract with --force to replace them"
        )
    backend, data = _complete(
        llm.notes_prompt(transcript(repo, memo_id)),
        schema=SCHEMA,
        parse=parse_notes,
        failure="extraction failed",
        unavailable="no extraction backend",
    )
    repo.save_extraction(memo_id, backend, data, clear_edited=force)
    return backend


@dataclass(frozen=True)
class Change:
    """One line a repair pass actually changed, and what it changed it from."""

    segment_id: int
    before: str
    after: str


@dataclass(frozen=True)
class RefineResult:
    """What a repair pass did: the lines it changed, the lines flagged — either
    a rewrite the pass refused to accept, or a line a window never answered
    for — and the count it found nothing to fix in."""

    changes: list[Change]
    flagged: list[int]
    untouched: int


# how many repair windows are asked for at once. A window is almost entirely
# time spent waiting on a backend, so several together turn the minutes a long
# memo used to take into a fraction of that, while staying modest enough to be
# a fair neighbour on a subscription CLI. The local fallback queues requests on
# its own server regardless, so this can only ever help it.
REFINE_WORKERS: int = config.REFINE_WORKERS


def _repair_window(chunk: Chunk) -> dict[int, str]:
    """The repairs one window comes back with, keyed by the line they belong to.
    A window is answered on its own, which is what lets several be asked at
    once; a window nobody can answer ends the pass rather than leaving the
    transcript repaired in parts."""
    return _complete(
        llm.refine_prompt(chunk),
        schema=REFINE_SCHEMA,
        parse=partial(parse_refinements, expected_ids=chunk.target_ids),
        failure="refinement failed",
        unavailable="no refinement backend",
        model=config.REFINE_MODEL,
    )[1]


def refine_transcript(repo: Repository, memo_id: int, dry_run: bool = False) -> RefineResult:
    """Repairs transcription errors a window at a time, several windows at once,
    reading each line in the company of its neighbours. A line the model changed
    past recognition, or never answered for at all, keeps the words that were
    actually transcribed and is reported instead — a weaker model that leaves
    lines out of a reply must not fail the whole pass over it. A pass replaces
    the memo's whole refinement, so running it again reverts any line this pass
    does not repair the same way."""
    require_memo(repo, memo_id)
    segments = repo.segments(memo_id)
    chunks = chunk_segments(segments, chunk_size=config.REFINE_WINDOW)
    with ThreadPoolExecutor(max_workers=REFINE_WORKERS) as pool:
        # map hands the replies back in window order, which is the order
        # merge_refinements pairs them against the windows in
        replies = list(pool.map(_repair_window, chunks))
    targeted = {tid for chunk in chunks for tid in chunk.target_ids}
    final, flagged = accept_repairs(segments, merge_refinements(chunks, replies), targeted)
    changes = [
        Change(s.id, s.text, final[s.id])
        for s in segments
        if s.id is not None and final[s.id] != s.text
    ]
    if not dry_run:
        repo.update_refinements(memo_id, {c.segment_id: c.after for c in changes})
    return RefineResult(changes, flagged, len(segments) - len(changes) - len(flagged))


def refine_diff_text(result: RefineResult) -> str:
    """The repairs laid out for a person to check before they are kept."""
    return "\n".join(f"[{c.segment_id}] {c.before}\n      → {c.after}" for c in result.changes)


def _extraction(repo: Repository, memo_id: int) -> Extraction:
    """Fetches a memo's notes, telling the user how to create them if missing."""
    extraction = repo.extraction(memo_id)
    if not extraction:
        raise NotFound(f"no extraction for memo {memo_id} — run: vtn extract {memo_id}")
    return extraction


def notes(repo: Repository, memo_id: int) -> str:
    """The notes as a person reads them."""
    return render_notes(_extraction(repo, memo_id))


def save_notes(repo: Repository, memo_id: int, markdown: str) -> None:
    """Keeps somebody's own wording for a memo's notes, over the top of whatever
    the model wrote. The extraction underneath is never touched, so `vtn notes
    --json` still round-trips exactly what the model produced and a script
    reading it cannot be fooled by an edit made for human eyes."""
    text = markdown.strip()
    if not text:
        raise InvalidInput("a note needs something in it")
    require_memo(repo, memo_id)
    repo.save_notes_md(memo_id, text)


def notes_markdown(repo: Repository, memo_id: int) -> str:
    """The notes as Markdown for a screen that renders them: what a person wrote
    if they have written anything, otherwise what the model made. A memo nobody
    has extracted yet gets a line saying so, since a screen has to show
    something where a command line can simply refuse."""
    edited = repo.notes_md(memo_id)
    if edited:
        return edited
    extraction = repo.extraction(memo_id)
    if not extraction:
        return "*no notes yet — run `vtn extract` on this memo*"
    return render_notes_markdown(extraction)


def notes_json(repo: Repository, memo_id: int) -> str:
    """The notes as a script reads them."""
    return json.dumps(_extraction(repo, memo_id).data, ensure_ascii=False)


def transcript_json(repo: Repository, memo_id: int, *, raw: bool = False) -> str:
    """The transcript as a script reads it, speakers already named. Carries each
    line's repair where one exists; raw carries the transcription instead."""
    segments = segments_as_dicts(
        repo.segments(memo_id), repo.display_names(memo_id), raw=raw
    )
    return json.dumps(segments, ensure_ascii=False)


def projects(repo: Repository) -> list[tuple[str, int]]:
    """Every project and how full it is, for a sidebar to list."""
    return repo.projects()


def memos(
    repo: Repository, project: str | None = None, tag: str | None = None
) -> list[Memo]:
    """The stored memos as records, for a screen that lays out its own rows.
    Every listing goes through here, so a tag is tidied and refused in one place
    however it was asked for."""
    return repo.memos(project, _tag(tag))


# what a table of memos puts beside each name, in the order a reader scans it.
# The screen takes its headings from here rather than naming them itself, so the
# columns and the values under them cannot come to disagree.
MEMO_COLUMNS = ("name", "duration", "speakers", "status", "created", "updated")


@dataclass(frozen=True)
class MemoRow:
    """One memo as a table lists it: the id its row is keyed by, and a value for
    each of MEMO_COLUMNS, in that order."""

    id: int
    name: str
    duration: str
    speakers: str
    status: str
    created: str
    updated: str


def _status(status: str, refined: bool, edited: bool) -> str:
    """How far a memo got, and what has been done to it by hand since. The info
    modal has a line each for repaired and edited; a table has one column, so
    they ride along with the status they qualify, named the same either way."""
    marks = [name for name, on in (("repaired", refined), ("edited", edited)) if on]
    return f"{status} ({', '.join(marks)})" if marks else status


def memo_rows(
    repo: Repository, project: str | None = None, tag: str | None = None
) -> list[MemoRow]:
    """The memo list as a table lays it out, narrowed as the caller asked: a row
    per memo, saying the same about each one as its info would, and gathered in
    a single query so that a list of a hundred memos costs what a list of one
    does. A memo nobody has changed reads as updated when it was made, exactly
    as the info does, since a column has to say something."""
    return [
        MemoRow(
            id=listed.memo.id,
            name=listed.memo.filename,
            duration=_duration(listed.memo.duration_s),
            speakers=str(listed.speakers),
            status=_status(listed.memo.status, listed.refined, listed.edited),
            created=listed.memo.created_at,
            updated=listed.memo.updated_at or listed.memo.created_at,
        )
        for listed in repo.memo_listings(project, _tag(tag))
    ]


def memos_json(
    repo: Repository, project: str | None = None, tag: str | None = None
) -> str:
    """The memo list as a script reads it, narrowed as the caller asked."""
    return json.dumps(
        [asdict(m) for m in memos(repo, project, tag)], ensure_ascii=False
    )


def question(text: str) -> str:
    """A question with its spacing tidied, refusing one that is really empty: an
    empty question still costs a call to a model, which then answers whatever it
    makes of being asked nothing. Public so a screen can refuse one while its
    modal is still open, rather than after it has closed on the typing."""
    asked = text.strip()
    if not asked:
        raise InvalidInput("a question needs something in it")
    return asked


def ask(repo: Repository, memo_id: int, asked: str) -> tuple[str, str]:
    """Answers a question about one memo, grounded only in what was said. The
    answer is stored nowhere: it is read once and gone."""
    text = question(asked)
    require_memo(repo, memo_id)
    return _complete(
        llm.ask_prompt(transcript(repo, memo_id), text),
        schema=None,
        parse=str.strip,
        failure="ask failed",
        unavailable="no backend",
    )


def speakers(repo: Repository, memo_id: int) -> dict[str, str]:
    """Each voice in a memo and what it is currently called, for a screen asking
    which of them to put a name to."""
    return repo.display_names(memo_id)


def move_memo(repo: Repository, memo_id: int, project: str) -> None:
    """Files a memo under a different project, so it groups with its own work."""
    name = _project_name(project)
    require_memo(repo, memo_id)
    repo.set_project(memo_id, name)


@dataclass(frozen=True)
class MemoInfo:
    """What state one memo is in, for a screen or a command line to lay out."""

    filename: str
    project: str
    duration: str
    language: str
    status: str
    created: str
    updated: str
    speakers: int
    refined: bool
    edited: bool


def memo_info(repo: Repository, memo_id: int) -> MemoInfo:
    """What a reader asks about one memo: where it is filed, how far through the
    pipeline it got, and whether anybody has repaired its transcript or written
    its notes over. A memo nobody has changed reads as updated when it was made,
    since a screen has to print something and "never" would misdescribe a
    recording that has been sitting there correct since the day it arrived."""
    memo = require_memo(repo, memo_id)
    return MemoInfo(
        filename=memo.filename,
        project=memo.project,
        duration=_duration(memo.duration_s),
        language=memo.language or "?",
        status=memo.status,
        created=memo.created_at,
        updated=memo.updated_at or memo.created_at,
        speakers=len(repo.display_names(memo_id)),
        refined=any(s.refined_text is not None for s in repo.segments(memo_id)),
        edited=bool(repo.notes_md(memo_id)),
    )


def _yes_no(flag: bool) -> str:
    """A plain answer where a reader would otherwise be shown True."""
    return "yes" if flag else "no"


def memo_info_text(repo: Repository, memo_id: int) -> str:
    """The same as a person reads it, labels padded so the values line up. The
    screen shows these lines rather than laying out its own, so the two cannot
    come to disagree about what a memo's state is called."""
    info = memo_info(repo, memo_id)
    rows = [
        ("file", info.filename),
        ("project", info.project),
        ("length", info.duration),
        ("language", info.language),
        ("status", info.status),
        ("created", info.created),
        ("updated", info.updated),
        ("speakers", str(info.speakers)),
        ("repaired", _yes_no(info.refined)),
        ("edited", _yes_no(info.edited)),
    ]
    return "\n".join(f"{label + ':':<10}{value}" for label, value in rows)


def project_name(name: str) -> str:
    """A project name as it will be stored, for a screen that has to go looking
    for what it just renamed under the name the rename actually used."""
    return _project_name(name)


def rename_project(repo: Repository, old: str, new: str) -> int:
    """Renames a project by refiling every memo in it, reporting how many moved.
    A name already in use is a merge rather than a clash: projects are only a
    value on a memo, so two piles answering to one name are one pile."""
    name = _project_name(new)
    moved = repo.refile_project(old, name)
    if not moved:
        raise NotFound(f"no project {old}")
    return moved


def remove_project(repo: Repository, name: str) -> int:
    """Empties a project by filing everything in it under 'other', reporting how
    many moved. A project with nothing in it has already stopped existing, so
    removing one is that bulk refiling and nothing besides."""
    if name == "other":
        raise InvalidInput("other is where emptied projects go")
    moved = repo.refile_project(name, "other")
    if not moved:
        raise NotFound(f"no project {name}")
    return moved


def rename_speaker(repo: Repository, memo_id: int, label: str, name: str) -> None:
    """Puts a real name on a speaker so later memos recognise that voice. A name
    of nothing is refused: empty is not the same as unnamed to the database, so
    a speaker called "" would count as named and join the pool of known voices
    that later recordings are matched against."""
    speaker = name.strip()
    if not speaker:
        raise InvalidInput("a speaker needs a name")
    if not repo.rename_speaker(memo_id, label, speaker):
        raise NotFound(f"no speaker {label} in memo {memo_id}")
