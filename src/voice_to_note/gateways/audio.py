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


def merge_tracks(system: Path, mic: Path, dst: Path) -> None:
    """Folds a taped meeting's two tracks — what the Mac played and what the
    microphone heard — into the one recording the rest of the app knows how to
    read. The longest track sets the length, so a side that kept talking after
    the other went quiet is not cut short. Failures name both source files in
    full: they are the only copy of the meeting until this succeeds."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(system),
                "-i", str(mic),
                "-filter_complex", "amix=inputs=2:duration=longest",
                "-c:a", "aac", "-b:a", "128k",
                str(dst),
            ],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise GatewayError(f"ffmpeg not found — {INSTALL_HINT}\ntracks kept: {system}, {mic}") from e
    except subprocess.TimeoutExpired as e:
        raise GatewayError(
            f"ffmpeg timed out after {TIMEOUT_S}s merging {system} and {mic}"
        ) from e
    if proc.returncode != 0:
        raise GatewayError(
            f"ffmpeg failed merging {system} and {mic}:\n{proc.stderr[-2000:]}"
        )


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
