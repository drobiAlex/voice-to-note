import json
import subprocess
from pathlib import Path


def to_wav16k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-af", "loudnorm",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(dst),
        ],
        check=True,
    )


def duration_seconds(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])
