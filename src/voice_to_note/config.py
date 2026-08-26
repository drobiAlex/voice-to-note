import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar


def home(env: Mapping[str, str]) -> Path:
    """Where the app keeps everything (models, data, config): VTN_HOME if
    set, else the macOS application-support directory. Read from the
    environment only — vtn.toml lives inside this directory, so it cannot
    name its own location."""
    raw = env.get("VTN_HOME")
    return Path(raw) if raw else Path.home() / "Library" / "Application Support" / "vtn"


ROOT = home(os.environ)
VENDOR = ROOT / "vendor" / "whisper.cpp"
WHISPER_BIN = VENDOR / "build" / "bin" / "whisper-cli"
MODELS_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
RECORDINGS_DIR = DATA_DIR / "recordings"
DB_PATH = DATA_DIR / "voice_to_note.db"
CONFIG_PATH = ROOT / "vtn.toml"
TEMPLATES_DIR = ROOT / "templates"

# the meeting-capture helper ships as source inside this package and is compiled
# into VTN_HOME by setup, since a native binary cannot ride along in a wheel
NATIVE_DIR = Path(__file__).resolve().parent / "native"
CAPTURE_SRC = NATIVE_DIR / "capture.swift"
CAPTURE_PLIST = NATIVE_DIR / "Info.plist"
CAPTURE_BIN = ROOT / "bin" / "vtn-capture"

# the menu bar recorder is built as a real app bundle rather than a bare binary:
# macOS attributes microphone and system-audio permission to the bundle a
# recording is launched from, and only a bundle carries the Info.plist whose
# wording the permission prompt then shows
MENUBAR_SRC = NATIVE_DIR / "menubar.swift"
MENUBAR_PLIST = NATIVE_DIR / "menubar-Info.plist"
MENUBAR_APP = ROOT / "bin" / "VTN Recorder.app"
MENUBAR_BIN = MENUBAR_APP / "Contents" / "MacOS" / "vtn-menubar"

# where setup records a content hash of each helper's source once it has built
# it, so a later run can tell a stale binary from a current one. Kept beside
# the binary rather than inside the .app bundle: the bundle is codesigned as a
# whole, and a stray file added inside it afterward would break that seal.
CAPTURE_STAMP = ROOT / "bin" / "vtn-capture.built"
MENUBAR_STAMP = ROOT / "bin" / "vtn-menubar.built"

T = TypeVar("T")


def read_config_file(path: Path) -> dict[str, Any]:
    """Reads the project's optional settings file, if the user wrote one."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def resolve(
    key: str,
    default: T,
    cast: Callable[[Any], T],
    env: Mapping[str, str],
    settings: Mapping[str, Any],
) -> T:
    """VTN_<KEY> in the environment wins, then vtn.toml, then the default."""
    raw = env.get(f"VTN_{key.upper()}")
    if raw is None:
        raw = settings.get(key)
    return default if raw is None else cast(raw)


def source(key: str, env: Mapping[str, str], settings: Mapping[str, Any]) -> str:
    """Where a setting's effective value actually came from — the same order
    resolve() falls back through, named for a person to read rather than
    inferred by them from the value alone."""
    if f"VTN_{key.upper()}" in env:
        return "env"
    if key in settings:
        return "file"
    return "default"


@dataclass(frozen=True)
class Setting:
    """One configurable value, complete enough to resolve, to describe, and to
    edit: its default, how to parse it out of text, what it does, and what
    shape of value it takes. A default and its parser live in the same place,
    so they cannot drift apart. `kind` and `choices` drive an editor's UI
    only — config_set's cast stays the source of truth for what is actually
    accepted."""

    default: Any
    cast: Callable[[Any], Any]
    doc: str
    kind: str = "text"
    choices: tuple[str, ...] = ()


SETTINGS: dict[str, Setting] = {
    "whisper_model": Setting(
        "large-v3-turbo",
        str,
        "whisper.cpp model used to transcribe",
        kind="choice",
        choices=("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"),
    ),
    "emb_model": Setting(
        "nemo_en_titanet_large.onnx", str, "model used to embed a voice for speaker matching"
    ),
    "num_speakers": Setting(-1, int, "speaker count to assume; -1 auto-detects", kind="int"),
    "overlap_stages": Setting(
        "auto",
        str,
        "run speaker detection alongside transcription rather than after it",
        kind="choice",
        choices=("auto", "on", "off"),
    ),
    "diar_threshold": Setting(
        0.5,
        float,
        "similarity above which two turns count as the same speaker",
        kind="float01",
    ),
    "match_threshold": Setting(
        0.5,
        float,
        "similarity above which a voice matches a known speaker",
        kind="float01",
    ),
    "claude_model": Setting(
        "sonnet",
        str,
        "claude model used for extraction and Q&A",
        kind="choice",
        choices=("sonnet", "haiku", "opus"),
    ),
    "refine_model": Setting(
        "haiku",
        str,
        "claude model used for transcript repair",
        kind="choice",
        choices=("sonnet", "haiku", "opus"),
    ),
    "ollama_url": Setting(
        "http://localhost:11434", str, "ollama server used as a local fallback", kind="url"
    ),
    "ollama_model": Setting("qwen3:8b", str, "ollama model used as a local fallback"),
    "llm_backends": Setting(
        "claude,ollama",
        str,
        "comma-ordered LLM backends to try",
        kind="multichoice",
        choices=("claude", "codex", "gemini", "ollama"),
    ),
    "codex_model": Setting("", str, "codex model override; empty uses the CLI's own default"),
    "gemini_model": Setting("", str, "gemini model override; empty uses the CLI's own default"),
    "claude_timeout_s": Setting(
        600, int, "seconds before a stuck claude call is given up on", kind="int"
    ),
    "ollama_timeout_s": Setting(
        1800, int, "seconds before a stuck ollama call is given up on", kind="int"
    ),
    "codex_timeout_s": Setting(
        600, int, "seconds before a stuck codex call is given up on", kind="int"
    ),
    "gemini_timeout_s": Setting(
        600, int, "seconds before a stuck gemini call is given up on", kind="int"
    ),
    "refine_workers": Setting(4, int, "repair windows a refine pass runs at once", kind="int"),
    "refine_window": Setting(
        20, int, "transcript lines repaired together in one refine window", kind="int"
    ),
    "whisper_repo_url": Setting(
        "https://github.com/ggml-org/whisper.cpp",
        str,
        "whisper.cpp source cloned during setup",
        kind="url",
    ),
    # an owner comes off a transcript as whatever the speakers called somebody,
    # so this is the name to recognise rather than an account of any kind — one
    # machine, one person, and a list of everybody's work readable as their own
    "my_name": Setting(
        "Alex", str, "the name you go by in memos; a first name is enough to claim a task"
    ),
    # Textual's built-in theme names, copied rather than imported: every
    # command reads this module, and only `vtn tui` is worth the cost of
    # importing Textual. A name Textual later drops falls back at start-up
    # rather than breaking the app, so the copy going stale is survivable
    "tui_theme": Setting(
        "textual-dark",
        str,
        "colour theme the TUI opens with",
        kind="choice",
        choices=(
            "ansi-dark", "ansi-light", "atom-one-dark", "atom-one-light",
            "catppuccin-frappe", "catppuccin-latte", "catppuccin-macchiato",
            "catppuccin-mocha", "dracula", "flexoki", "gruvbox", "monokai", "nord",
            "rose-pine", "rose-pine-dawn", "rose-pine-moon", "solarized-dark",
            "solarized-light", "textual-dark", "textual-light", "tokyo-night",
        ),
    ),
}

_SETTINGS = read_config_file(CONFIG_PATH)


def _setting(key: str) -> Any:
    """One setting, resolved against this machine's environment and config
    file by way of what SETTINGS says about it."""
    setting = SETTINGS[key]
    return resolve(key, setting.default, setting.cast, os.environ, _SETTINGS)


WHISPER_MODEL: str = _setting("whisper_model")
WHISPER_MODEL_PATH = MODELS_DIR / f"ggml-{WHISPER_MODEL}.bin"
VAD_MODEL_PATH = MODELS_DIR / "ggml-silero-v5.1.2.bin"

SEG_MODEL_PATH = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL: str = _setting("emb_model")
EMB_MODEL_PATH = MODELS_DIR / EMB_MODEL

NUM_SPEAKERS: int = _setting("num_speakers")
OVERLAP_STAGES: str = _setting("overlap_stages")
DIAR_THRESHOLD: float = _setting("diar_threshold")

MATCH_THRESHOLD: float = _setting("match_threshold")

CLAUDE_MODEL: str = _setting("claude_model")
REFINE_MODEL: str = _setting("refine_model")
OLLAMA_URL: str = _setting("ollama_url")
OLLAMA_MODEL: str = _setting("ollama_model")

LLM_BACKENDS: str = _setting("llm_backends")
CODEX_MODEL: str = _setting("codex_model")
GEMINI_MODEL: str = _setting("gemini_model")

CLAUDE_TIMEOUT_S: int = _setting("claude_timeout_s")
OLLAMA_TIMEOUT_S: int = _setting("ollama_timeout_s")
CODEX_TIMEOUT_S: int = _setting("codex_timeout_s")
GEMINI_TIMEOUT_S: int = _setting("gemini_timeout_s")

REFINE_WORKERS: int = _setting("refine_workers")
REFINE_WINDOW: int = _setting("refine_window")

WHISPER_REPO_URL: str = _setting("whisper_repo_url")

MY_NAME: str = _setting("my_name")

TUI_THEME: str = _setting("tui_theme")
