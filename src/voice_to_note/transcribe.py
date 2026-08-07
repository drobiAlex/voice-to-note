import json
import subprocess
import tempfile
from pathlib import Path

from . import config


def transcribe(wav: Path) -> dict:
    if not config.WHISPER_BIN.exists():
        raise RuntimeError("whisper-cli not built — run ./run.sh first")
    if not config.WHISPER_MODEL_PATH.exists():
        raise RuntimeError(f"model missing: {config.WHISPER_MODEL_PATH} — run ./run.sh first")
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
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed:\n{proc.stderr[-2000:]}")
        return json.loads(out.with_suffix(".json").read_text())


def segments(raw: dict) -> list[dict]:
    segs = []
    for s in raw.get("transcription", []):
        text = s["text"].strip()
        if not text:
            continue
        segs.append(
            {
                "t0_ms": s["offsets"]["from"],
                "t1_ms": s["offsets"]["to"],
                "text": text,
                "tokens": s.get("tokens", []),
            }
        )
    return segs
