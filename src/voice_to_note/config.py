import os
import tomllib
from collections.abc import Callable, Mapping
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


_SETTINGS = read_config_file(CONFIG_PATH)


def _setting(key: str, default: T, cast: Callable[[Any], T]) -> T:
    """One setting, resolved against this machine's environment and config.
    Every setting states how to read it, so a default and its parser cannot
    drift apart."""
    return resolve(key, default, cast, os.environ, _SETTINGS)


WHISPER_MODEL = _setting("whisper_model", "large-v3-turbo", str)
WHISPER_MODEL_PATH = MODELS_DIR / f"ggml-{WHISPER_MODEL}.bin"
VAD_MODEL_PATH = MODELS_DIR / "ggml-silero-v5.1.2.bin"

SEG_MODEL_PATH = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = _setting("emb_model", "nemo_en_titanet_large.onnx", str)
EMB_MODEL_PATH = MODELS_DIR / EMB_MODEL

NUM_SPEAKERS = _setting("num_speakers", -1, int)
DIAR_THRESHOLD = _setting("diar_threshold", 0.5, float)

MATCH_THRESHOLD = _setting("match_threshold", 0.5, float)

CLAUDE_MODEL = _setting("claude_model", "sonnet", str)
REFINE_MODEL = _setting("refine_model", "haiku", str)
OLLAMA_URL = _setting("ollama_url", "http://localhost:11434", str)
OLLAMA_MODEL = _setting("ollama_model", "qwen3:8b", str)
