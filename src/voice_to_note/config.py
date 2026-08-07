import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "whisper.cpp"
WHISPER_BIN = VENDOR / "build" / "bin" / "whisper-cli"
MODELS_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "voice_to_note.db"

WHISPER_MODEL = os.environ.get("VTN_WHISPER_MODEL", "large-v3-turbo")
WHISPER_MODEL_PATH = MODELS_DIR / f"ggml-{WHISPER_MODEL}.bin"
VAD_MODEL_PATH = MODELS_DIR / "ggml-silero-v5.1.2.bin"
