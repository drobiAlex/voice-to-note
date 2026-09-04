"""Runs the real `process_memo` over a fixture and times every stage.

The stage boundaries come from the progress callback the pipeline already
reports, so this measures what the app does rather than a copy of it, and a
change to the ordering of the stages shows up here without this file changing.
The pipeline marks only converting, transcribing and diarizing, so the last
marked span necessarily covers diarization, speaker embeddings and the memo's
inserts together — it is printed as diarize+store so nobody optimises
"diarizing" for time that was really spent in SQL. Runs once with the model
stages taking turns and once overlapped, which is the comparison
services._overlapping's docstring sends a reader here for.

Run: VTN_HOME=$PWD .venv/bin/python bench/pipeline.py
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_to_note import config, services  # noqa: E402
from voice_to_note.storage.repository import Repository  # noqa: E402

FIXTURE = Path(__file__).parent / "set" / "speech-3.m4a"
# what the span between two progress marks actually contains, where that is
# more than the mark's own name says
LABELS = {"diarizing": "diarize+store"}


def once(db: Path, overlap: str) -> None:
    """One full run of the pipeline under one overlap mode. Set the same way
    tests set config: the constant resolved at import, so the env var would
    change nothing this late."""
    config.OVERLAP_STAGES = overlap
    marks: list[tuple[str, float]] = []

    def progress(_step: int, stage: str) -> None:
        marks.append((stage, time.perf_counter()))

    with Repository(str(db)) as repo:
        start = time.perf_counter()
        result = services.process_memo(
            repo, FIXTURE, project="bench", progress=progress, num_speakers=3
        )
        marks.append(("close", time.perf_counter()))
    marks.append(("done", time.perf_counter()))
    total = marks[-1][1] - start

    print(f"\n{FIXTURE.name} — 8 min audio, 3 voices, overlap_stages={overlap}")
    for (stage, at), (_next, then) in zip(marks, marks[1:], strict=False):
        print(f"  {LABELS.get(stage, stage):14s}{then - at:8.1f}s")
    print(f"  {'TOTAL':14s}{total:8.1f}s   "
          f"{result.segment_count} segments, {len(result.labels)} speakers")


def main() -> int:
    if not os.environ.get("VTN_HOME"):
        # without it config.DB_PATH resolves to the user's real memo library,
        # which the next lines would delete and rebuild
        print("refusing: VTN_HOME is not set, and this script deletes and rebuilds\n"
              "the database it points at.\n"
              "run: VTN_HOME=$PWD .venv/bin/python bench/pipeline.py")
        return 1
    if not FIXTURE.exists():
        print(f"missing {FIXTURE} — run bench/make_speech.py first")
        return 1
    db = Path(config.DB_PATH)
    db.unlink(missing_ok=True)
    for overlap in ("off", "on"):
        once(db, overlap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
