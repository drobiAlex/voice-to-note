import json
import subprocess
import tempfile
from pathlib import Path
from typing import cast

from .. import config
from ..transforms.segments import WhisperTranscription
from . import GatewayError

# transcription runs faster than real time even on CPU; this is a stuck-process
# guard, not a performance target
TIMEOUT_FACTOR = 4
TIMEOUT_FLOOR_S = 120


def timeout_for(duration_s: float) -> float:
    """How long to wait on a recording before calling transcription stuck."""
    return max(TIMEOUT_FLOOR_S, TIMEOUT_FACTOR * duration_s)


def transcribe(wav: Path, duration_s: float) -> WhisperTranscription:
    """Turns speech into timed text, locally."""
    if not config.WHISPER_BIN.exists():
        raise GatewayError("whisper-cli not built — run ./run.sh first")
    if not config.WHISPER_MODEL_PATH.exists():
        raise GatewayError(f"model missing: {config.WHISPER_MODEL_PATH} — run ./run.sh first")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        cmd = [
            str(config.WHISPER_BIN),
            "-m", str(config.WHISPER_MODEL_PATH),
            "-f", str(wav),
            "-l", "auto",
            "-ojf",
            "-of", str(out),
            "-np",
        ]
        if config.VAD_MODEL_PATH.exists():
            cmd += ["--vad", "--vad-model", str(config.VAD_MODEL_PATH)]
        timeout = timeout_for(duration_s)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise GatewayError(
                f"whisper-cli timed out after {timeout:.0f}s on {wav.name}"
            ) from e
        if proc.returncode != 0:
            raise GatewayError(f"whisper-cli failed:\n{proc.stderr[-2000:]}")
        # whisper's own output, taken at its word: the segment reader names any
        # field it finds missing, while a missing language just reads as unset
        return cast(WhisperTranscription, json.loads(out.with_suffix(".json").read_text()))
