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

SEG_MODEL_PATH = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = os.environ.get("VTN_EMB_MODEL", "nemo_en_titanet_large.onnx")
EMB_MODEL_PATH = MODELS_DIR / EMB_MODEL

NUM_SPEAKERS = int(os.environ.get("VTN_NUM_SPEAKERS", "-1"))
DIAR_THRESHOLD = float(os.environ.get("VTN_DIAR_THRESHOLD", "0.5"))

CLAUDE_MODEL = os.environ.get("VTN_CLAUDE_MODEL", "sonnet")
OLLAMA_URL = os.environ.get("VTN_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("VTN_OLLAMA_MODEL", "qwen3:8b")
