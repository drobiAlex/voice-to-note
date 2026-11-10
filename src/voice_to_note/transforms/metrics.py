import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..domain import RunRecord


@dataclass(frozen=True)
class StageStats:
    """How one stage of one kind of run has been going, over every time it ran:
    typical and bad-day timings, how much processor it burned for its wall
    time, and how many times faster than the audio it got through it. Speed is
    the number that travels between machines and recordings — a 40 minute
    meeting taking 4 minutes and a 4 minute memo taking 24 seconds are the
    same machine behaving the same way."""

    kind: str
    stage: str
    count: int
    failed: int
    wall_median: float
    wall_p90: float
    wall_max: float
    cpu_median: float | None
    speed_median: float | None


@dataclass(frozen=True)
class Outcomes:
    """How every run of one kind ended."""

    kind: str
    ok: int
    failed: int
    cancelled: int
    unfinished: int


def percentile(values: Sequence[float], q: float) -> float:
    """The nearest-rank percentile: always a value that actually happened,
    which is what a person reading "p90 took 41s" will assume it is."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def stage_stats(runs: Iterable[RunRecord]) -> list[StageStats]:
    """Every stage of every kind of run, grouped and summarised, in the order
    the kinds and their stages were first seen running. Timings of stages that
    failed are left out of the medians — a stage that died in a second says
    nothing about how long it takes — but they are counted, since a stage that
    keeps failing is its own finding."""
    walls: dict[tuple[str, str], list[float]] = defaultdict(list)
    cpus: dict[tuple[str, str], list[float]] = defaultdict(list)
    speeds: dict[tuple[str, str], list[float]] = defaultdict(list)
    failed: dict[tuple[str, str], int] = defaultdict(int)
    for run in runs:
        for st in run.stages:
            key = (run.kind, st.stage)
            if st.outcome != "ok":
                failed[key] += 1
                walls.setdefault(key, [])
                continue
            walls[key].append(st.wall_s)
            if st.cpu_s is not None:
                cpus[key].append(st.cpu_s)
            if run.audio_s and st.wall_s > 0:
                speeds[key].append(run.audio_s / st.wall_s)
    return [
        StageStats(
            kind=kind,
            stage=stage,
            count=len(timed) + failed[(kind, stage)],
            failed=failed[(kind, stage)],
            wall_median=percentile(timed, 0.5) if timed else 0.0,
            wall_p90=percentile(timed, 0.9) if timed else 0.0,
            wall_max=max(timed, default=0.0),
            cpu_median=percentile(cpus[(kind, stage)], 0.5) if cpus[(kind, stage)] else None,
            speed_median=(
                percentile(speeds[(kind, stage)], 0.5) if speeds[(kind, stage)] else None
            ),
        )
        for (kind, stage), timed in walls.items()
    ]


def outcomes(runs: Iterable[RunRecord]) -> list[Outcomes]:
    """How each kind of run has been ending, kinds in the order first seen."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for run in runs:
        counts[run.kind][run.outcome or "unfinished"] += 1
    return [
        Outcomes(kind, c["ok"], c["failed"], c["cancelled"], c["unfinished"])
        for kind, c in counts.items()
    ]


def slowest(runs: Iterable[RunRecord], n: int) -> list[RunRecord]:
    """The finished runs that took longest on the clock, worst first. Ranked
    by wall time alone rather than by speed, because what is being looked for
    is the wait somebody sat through; the speed is printed beside it to say
    whether that wait was the audio's length or the machine's fault."""
    finished = [run for run in runs if run.wall_s is not None]
    return sorted(finished, key=lambda run: run.wall_s or 0.0, reverse=True)[:n]
