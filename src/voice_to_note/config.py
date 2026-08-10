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
DB_PATH = DATA_DIR / "voice_to_note.db"
CONFIG_PATH = ROOT / "vtn.toml"
TEMPLATES_DIR = ROOT / "templates"

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
    """One configurable value, complete enough to resolve and to describe: its
    default, how to parse it out of text, and what it does. A default and its
    parser live in the same place, so they cannot drift apart."""

    default: Any
    cast: Callable[[Any], Any]
    doc: str


SETTINGS: dict[str, Setting] = {
    "whisper_model": Setting("large-v3-turbo", str, "whisper.cpp model used to transcribe"),
    "emb_model": Setting(
        "nemo_en_titanet_large.onnx", str, "model used to embed a voice for speaker matching"
    ),
    "num_speakers": Setting(-1, int, "speaker count to assume; -1 auto-detects"),
    "diar_threshold": Setting(
        0.5, float, "similarity above which two turns count as the same speaker"
    ),
    "match_threshold": Setting(
        0.5, float, "similarity above which a voice matches a known speaker"
    ),
    "claude_model": Setting("sonnet", str, "claude model used for extraction and Q&A"),
    "refine_model": Setting("haiku", str, "claude model used for transcript repair"),
    "ollama_url": Setting("http://localhost:11434", str, "ollama server used as a local fallback"),
    "ollama_model": Setting("qwen3:8b", str, "ollama model used as a local fallback"),
    "llm_backends": Setting("claude,ollama", str, "comma-ordered LLM backends to try"),
    "codex_model": Setting("", str, "codex model override; empty uses the CLI's own default"),
    "gemini_model": Setting("", str, "gemini model override; empty uses the CLI's own default"),
    "claude_timeout_s": Setting(600, int, "seconds before a stuck claude call is given up on"),
    "ollama_timeout_s": Setting(1800, int, "seconds before a stuck ollama call is given up on"),
    "codex_timeout_s": Setting(600, int, "seconds before a stuck codex call is given up on"),
    "gemini_timeout_s": Setting(600, int, "seconds before a stuck gemini call is given up on"),
    "refine_workers": Setting(4, int, "repair windows a refine pass runs at once"),
    "refine_window": Setting(20, int, "transcript lines repaired together in one refine window"),
    "whisper_repo_url": Setting(
        "https://github.com/ggml-org/whisper.cpp", str, "whisper.cpp source cloned during setup"
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
