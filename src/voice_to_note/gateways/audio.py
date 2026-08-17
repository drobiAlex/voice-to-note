import json
import subprocess
from datetime import UTC, datetime
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


def recorded_at(path: Path) -> str:
    """When a recording was actually made, as the container itself records it.
    That instant is the one thing about a recording that survives being copied,
    renamed and handed around, which makes it the only sound way to tell a file
    already imported from a genuinely new one — filenames collide between
    unrelated memos and differ between copies of the same one.

    A container carrying no creation time falls back to when the file was last
    written. That is a weaker answer, since copying can rewrite it, but it is
    always an answer: a missing tag is an ordinary recording rather than a
    broken one, and only ffprobe itself failing is worth an error."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=creation_time",
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
        tag = json.loads(proc.stdout)["format"]["tags"]["creation_time"]
    except (ValueError, KeyError, TypeError):
        tag = None
    return _tagged(tag) or _utc(datetime.fromtimestamp(path.stat().st_mtime, UTC))


def _tagged(tag: object) -> str | None:
    """A container's creation-time tag as a comparable stamp, or nothing when
    there was no tag or it is written in some spelling this cannot read.
    Unparsable is not an error here: recorders disagree about the format, and a
    stamp nobody can read leaves the caller exactly where a missing one does."""
    if not isinstance(tag, str) or not tag:
        return None
    try:
        when = datetime.fromisoformat(tag)
    except ValueError:
        return None
    # a tag written without a zone is taken as the container's own UTC reckoning
    # rather than as local time: reading it as local would stamp the same file
    # differently on two machines, and an import is only recognised again by a
    # key that does not move
    return _utc(when if when.tzinfo else when.replace(tzinfo=UTC))


def _utc(when: datetime) -> str:
    """One spelling of an instant whatever clock stamped it: UTC, to the second.
    Recorders disagree below that — some write microseconds, some none — and an
    offset would let a single instant be written two ways, so a comparison of
    raw tags would miss recordings that were made at the very same moment."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
