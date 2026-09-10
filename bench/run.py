"""Times each stage of the pipeline over the fixture set, so an optimisation is
argued from a measurement rather than from a reading of the code.

Every stage that needs no model runs here; whisper and diarization are reported
as skipped when their binaries or models are absent, which is what happens on
CI and on a machine that has never run setup. The listing and read-back numbers
are taken against a library that grows as the set is processed, because that is
the cost a screen pays on every redraw and it is the one that scales with how
long somebody has been using the app.

Run: VTN_HOME=$PWD .venv/bin/python bench/run.py
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_to_note import config  # noqa: E402
from voice_to_note.domain import Speaker, Turn  # noqa: E402
from voice_to_note.gateways import GatewayError, audio, sherpa, whisper  # noqa: E402
from voice_to_note.storage.repository import Repository  # noqa: E402
from voice_to_note.transforms.segments import segments_from_whisper  # noqa: E402
from voice_to_note.transforms.speakers import assign_speakers  # noqa: E402

TIMES: dict[str, list[tuple[str, float]]] = {}
SKIPPED: dict[str, str] = {}


@contextmanager
def stage(recording: str, name: str):
    """One timed step. A stage that cannot run on this machine is recorded as
    skipped rather than as zero, so a missing model never reads as a fast one."""
    start = time.perf_counter()
    try:
        yield
    except GatewayError as missing:
        SKIPPED[name] = str(missing).split("\n")[0][:60]
        return
    TIMES.setdefault(recording, []).append((name, time.perf_counter() - start))


def fake_whisper(duration_s: float) -> dict:
    """A transcription the size the real one would be for this recording — one
    segment every four seconds — so the transforms and the writes downstream
    are measured on a realistic row count without a model having to run."""
    n = max(1, int(duration_s / 4))
    return {"transcription": [
        {"offsets": {"from": i * 4000, "to": i * 4000 + 3800},
         "text": f" segment number {i} of the recording"}
        for i in range(n)
    ], "result": {"language": "en"}}


def fake_turns(duration_s: float, speakers: int) -> list[Turn]:
    """Turns of the shape diarization returns, taking the floor in rotation.
    Turn speaks milliseconds — in seconds the turns would all crowd into the
    first moments of the recording and every segment past the first would take
    assign_speakers' fallback branch, timing a path the real pipeline only
    hits for gap segments."""
    return [Turn(i * 6000, i * 6000 + 6000, f"S{i % speakers + 1}")
            for i in range(max(1, int(duration_s / 6)))]


def process(repo: Repository, src: Path, speakers: int) -> int:
    """The pipeline as `process_memo` runs it, stage by stage."""
    name = src.stem
    wav = config.UPLOADS_DIR / f"{name}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)

    with stage(name, "convert"):
        audio.to_wav16k(src, wav)
    duration = 0.0
    with stage(name, "probe"):
        duration = audio.duration_seconds(wav)
    if not wav.exists() or not duration:
        # no ffmpeg on this machine: every later stage would read a wav that
        # was never made, so the fixture is skipped whole instead of dying
        # with a NameError halfway down
        return 0
    with stage(name, "transcribe"):
        raw = whisper.transcribe(wav, duration)
        del raw
    with stage(name, "diarize"):
        sherpa.diarize(wav, speakers)

    raw = fake_whisper(duration)
    with stage(name, "segments"):
        segs = segments_from_whisper(raw)
    turns = fake_turns(duration, speakers)
    with stage(name, "assign_speakers"):
        segs = assign_speakers(segs, turns)
    with stage(name, "create_memo"):
        memo_id = repo.create_memo(
            filename=src.name, wav_path=str(wav), duration_s=duration, language="en",
            segments=segs, speakers=[Speaker(f"S{i+1}") for i in range(speakers)],
            project="bench",
        )
    with stage(name, "read back"):
        repo.segments(memo_id)
    with stage(name, "listing"):
        repo.memo_listings(project="bench")
    return len(segs)


def main() -> int:
    if not os.environ.get("VTN_HOME"):
        # without it config.DB_PATH resolves to the user's real memo library,
        # which the next lines would delete and rebuild
        print("refusing: VTN_HOME is not set, and this script deletes and rebuilds\n"
              "the database it points at.\n"
              "run: VTN_HOME=$PWD .venv/bin/python bench/run.py")
        return 1
    fixtures = sorted((Path(__file__).parent / "set").glob("*.m4a"))
    if not fixtures:
        print("no fixtures — run bench/make_set.py first")
        return 1
    db = Path(config.DB_PATH)
    db.unlink(missing_ok=True)
    repo = Repository(str(db))
    rows = {}
    for src in fixtures:
        speakers = int(src.stem.rsplit("-", 1)[1])
        rows[src.stem] = process(repo, src, speakers)

    if TIMES:
        names = [n for n, _ in next(iter(TIMES.values()))]
        print(f"\n{'recording':14s}{'segs':>6s}" + "".join(f"{n:>16s}" for n in names))
        for recording, timings in TIMES.items():
            line = f"{recording:14s}{rows[recording]:6d}"
            for _n, seconds in timings:
                line += f"{seconds * 1000:13.1f} ms"
            print(line)
    if SKIPPED:
        print("\nskipped on this machine:")
        for name, why in SKIPPED.items():
            print(f"  {name:16s} {why}")
    print(f"\nlibrary: {len(fixtures)} memos, {sum(rows.values())} segments, "
          f"db {db.stat().st_size/1e6:.1f} MB")
    scale(repo)
    indexed(repo)
    return 0



def scale(repo: Repository) -> None:
    """What the listing costs as a library fills up. Called after the set has
    been processed, growing it to the size somebody reaches after a year of
    memos: the query a screen reruns on every redraw is the one whose cost
    nobody notices until it is already too slow to fix quietly."""
    from voice_to_note.transforms.segments import segments_from_whisper as _s
    profile = [7, 30, 120, 375]
    marks = (50, 100, 200)
    print(f"\n{'memos':>7s}{'segments':>10s}{'listing':>14s}")
    made = 4
    for target in marks:
        while made < target:
            segs = _s(fake_whisper(profile[made % len(profile)] * 4))
            repo.create_memo(
                filename=f"f{made}.m4a", wav_path=f"/tmp/f{made}.wav", duration_s=1.0,
                language="en", segments=segs,
                speakers=[Speaker("S1"), Speaker("S2")], project="bench",
            )
            made += 1
        best = min(_timed(repo) for _ in range(3))
        total = repo.con.execute("SELECT count(*) FROM segments").fetchone()[0]
        print(f"{made:7d}{total:10d}{best * 1000:11.1f} ms")


def _timed(repo: Repository) -> float:
    start = time.perf_counter()
    repo.memo_listings(project="bench")
    return time.perf_counter() - start


def indexed(repo: Repository) -> None:
    """The same listing once every foreign key carries an index."""
    repo.con.executescript(
        "CREATE INDEX IF NOT EXISTS segments_memo ON segments(memo_id);"
        "CREATE INDEX IF NOT EXISTS speakers_memo ON speakers(memo_id);"
        "CREATE INDEX IF NOT EXISTS todos_memo ON todos(memo_id);"
        "CREATE INDEX IF NOT EXISTS extractions_memo ON extractions(memo_id);"
    )
    repo.con.commit()
    best = min(_timed(repo) for _ in range(3))
    print(f"{'indexed':>7s}{'':10s}{best * 1000:11.1f} ms")

if __name__ == "__main__":
    sys.exit(main())
