import json
import subprocess
from pathlib import Path

from . import GatewayError

TIMEOUT_S = 600
INSTALL_HINT = "install ffmpeg: brew install ffmpeg"


def to_wav16k(src: Path, dst: Path) -> None:
    """Normalizes any recording into the one audio format the models accept."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src),
                "-af", "loudnorm",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(dst),
            ],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise GatewayError(f"ffmpeg not found — {INSTALL_HINT}") from e
    except subprocess.TimeoutExpired as e:
        raise GatewayError(f"ffmpeg timed out after {TIMEOUT_S}s converting {src}") from e
    if proc.returncode != 0:
        raise GatewayError(f"ffmpeg failed converting {src.name}:\n{proc.stderr[-2000:]}")


def duration_seconds(path: Path) -> float:
    """How long a recording runs, which sets the transcription time budget."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise GatewayError(f"ffprobe not found — {INSTALL_HINT}") from e
    except subprocess.TimeoutExpired as e:
        raise GatewayError(f"ffprobe timed out after {TIMEOUT_S}s reading {path}") from e
    if proc.returncode != 0:
        raise GatewayError(f"ffprobe failed reading {path.name}:\n{proc.stderr[-2000:]}")
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as e:
        raise GatewayError(
            f"ffprobe reported no usable duration for {path.name}: {proc.stdout[:200]!r}"
        ) from e
