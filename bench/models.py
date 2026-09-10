"""Times the two model stages against the speech fixtures.

Kept apart from `run.py` because these are the stages that need `vtn setup` to
have finished, and because diarization spawns a process — which re-imports the
module it was started from, so this has to be a file rather than a script piped
in on stdin. A machine where only half of setup ran still prints a row per
stage: each model call is guarded on its own, so a missing diarizer reads as
`skipped` instead of killing the run before the later fixtures report.
"""

import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_to_note.gateways import GatewayError, sherpa, whisper  # noqa: E402
from voice_to_note.transforms.speakers import select_fingerprint_turns  # noqa: E402

FIXTURES = (("speech-1", 1), ("speech-3", 3), ("speech-5", 5))

# ru_maxrss is bytes on macOS but kilobytes on everything else
RSS_PER_MB = 1e6 if sys.platform == "darwin" else 1e3


def child_peak_mb() -> float:
    """The high-water mark over every child reaped so far — whisper-cli
    included once it has run — monotonic by definition. Printed with a label
    that says so, because a per-stage figure cannot be had from rusage."""
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / RSS_PER_MB


def main() -> int:
    print(f"{'fixture':10s}{'audio':>8s}{'stage':>12s}{'time':>10s}{'xRT':>8s}{'result':>14s}")
    for name, speakers in FIXTURES:
        wav = Path(__file__).parent / "set" / f"{name}.wav"
        if not wav.exists():
            continue
        seconds = wav.stat().st_size / (16000 * 2)

        try:
            start = time.perf_counter()
            raw = whisper.transcribe(wav, seconds)
            took = time.perf_counter() - start
            n = len(raw.get("transcription", []))
            print(f"{name:10s}{seconds:7.0f}s{'transcribe':>12s}{took:9.1f}s"
                  f"{seconds / took:7.1f}x{n:9d} segs")
        except GatewayError as missing:
            print(f"{name:10s}{seconds:7.0f}s{'transcribe':>12s}{'skipped':>10s}   {missing}")

        try:
            start = time.perf_counter()
            turns = sherpa.diarize(wav, speakers)
            took = time.perf_counter() - start
            print(f"{name:10s}{seconds:7.0f}s{'diarize':>12s}{took:9.1f}s"
                  f"{seconds / took:7.1f}x{len(turns):9d} turns"
                  f"   children peak so far {child_peak_mb():5.0f} MB")
        except GatewayError as missing:
            print(f"{name:10s}{seconds:7.0f}s{'diarize':>12s}{'skipped':>10s}   {missing}")
            continue

        # the gateway buckets by speaker and budgets each one on its own, so
        # the turns are handed over raw — exactly as services.py does — and
        # the reported count mirrors the selection the gateway will make
        by_speaker: dict[str, list] = {}
        for t in turns:
            by_speaker.setdefault(t.speaker, []).append(t)
        used = sum(len(select_fingerprint_turns(ts, int(seconds * 16000)))
                   for ts in by_speaker.values())
        try:
            start = time.perf_counter()
            sherpa.speaker_embeddings(wav, turns)
            took = time.perf_counter() - start
            print(f"{name:10s}{seconds:7.0f}s{'embed':>12s}{took:9.1f}s"
                  f"{seconds / took:7.1f}x{used:9d} turns used")
        except GatewayError as missing:
            print(f"{name:10s}{seconds:7.0f}s{'embed':>12s}{'skipped':>10s}   {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
