import json
import subprocess
import tempfile
from pathlib import Path
from typing import cast

from .. import config
from ..transforms.segments import WhisperTranscription
from . import GatewayError, qos

# transcription runs faster than real time even on CPU; this is a stuck-process
# guard, not a performance target
TIMEOUT_FACTOR = 4
TIMEOUT_FLOOR_S = 120


def timeout_for(duration_s: float) -> float:
    """How long to wait on a recording before calling transcription stuck."""
    return max(TIMEOUT_FLOOR_S, TIMEOUT_FACTOR * duration_s)


def require() -> None:
    """Raises unless everything transcription needs is on disk. Separate from
    the call itself so that a caller which starts another stage first can find
    out in a millisecond that this one was never going to run, rather than
    after minutes of work it then has to throw away."""
    if not config.WHISPER_BIN.exists():
        raise GatewayError("whisper-cli not built — run ./run.sh first")
    if not config.WHISPER_MODEL_PATH.exists():
        raise GatewayError(f"model missing: {config.WHISPER_MODEL_PATH} — run ./run.sh first")


def decoding() -> list[str]:
    """The flags that decide how hard whisper works for its words.

    Left to itself the binary searches five beams on four threads whatever
    machine it is on. Four threads leaves most of a laptop idle, and the beam
    width is the one setting that buys speed by changing the answer: greedy
    decoding is measurably faster and reads a little differently — the same
    words, punctuated and split into segments its own way. So the thread count
    follows the machine by default and the beam width does not move unless
    somebody has compared the two on their own recordings."""
    threads = config.WHISPER_THREADS
    count = int(threads) if threads.strip().isdigit() else qos.performance_cores()
    flags = ["-t", str(max(1, count))]
    beam = config.WHISPER_BEAM_SIZE
    # -bo as well as -bs: a beam of one still samples several candidates and
    # picks the best unless best-of is brought down with it
    return flags + (["-bs", "1", "-bo", "1"] if beam <= 1 else ["-bs", str(beam)])


def transcribe(wav: Path, duration_s: float) -> WhisperTranscription:
    """Turns speech into timed text, locally."""
    require()
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
            *decoding(),
        ]
        if config.VAD_MODEL_PATH.exists():
            cmd += ["--vad", "--vad-model", str(config.VAD_MODEL_PATH)]
        cmd = qos.background(cmd)
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
