import json
import subprocess
from pathlib import Path

TIMEOUT_S = 600


def to_wav16k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(src),
                "-af", "loudnorm",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(dst),
            ],
            check=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {TIMEOUT_S}s converting {src}") from e


def duration_seconds(path: Path) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=TIMEOUT_S,
        ).stdout
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffprobe timed out after {TIMEOUT_S}s reading {path}") from e
    return float(json.loads(out)["format"]["duration"])
