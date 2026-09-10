"""Builds the fixture recordings the optimisation runs are measured against.

Synthetic on purpose: the set has to be identical on every machine it is run
on, and a real meeting cannot be committed or reproduced. Each speaker is a
band of pink noise around a fundamental of its own, gated so that only one of
them talks at a time — enough structure for the VAD to find speech and for
diarization to have turns to separate, without pretending to be words.

Written as 48 kHz stereo m4a because that is what a phone or the recorder
hands `vtn process`, so the conversion stage is measured on the real thing.
"""

import subprocess
import sys
from pathlib import Path

# (name, seconds, speakers) — size crossed with how many voices are in it, so a
# regression that only shows up on long recordings or on crowded ones is still
# caught by one run of the set
SET = [
    ("short-1", 30, 1),
    ("brief-2", 120, 2),
    ("standup-3", 480, 3),
    ("meeting-5", 1500, 5),
]

TURN_S = 6          # how long each voice holds the floor
F0 = (110, 180, 145, 210, 95)   # a fundamental per speaker, low to high


def track(index: int, speakers: int, seconds: int) -> str:
    """One speaker's whole track, silent except on its own turns. Gating with
    an expression rather than cutting and concatenating keeps this to a single
    ffmpeg call however long the recording is."""
    f0 = F0[index % len(F0)]
    turn = f"eq(mod(floor(t/{TURN_S})\\,{speakers})\\,{index})"
    return (
        f"anoisesrc=d={seconds}:c=pink:r=48000:a=0.5,"
        f"bandpass=f={f0 * 3}:width_type=h:w={f0 * 2},"
        f"tremolo=f=4.5:d=0.7,"
        f"volume='if({turn}\\,1\\,0)':eval=frame"
    )


def build(out: Path, seconds: int, speakers: int) -> None:
    """One recording, mixed down from a track per speaker."""
    parts = [track(i, speakers, seconds) for i in range(speakers)]
    graph = ";".join(f"{p}[s{i}]" for i, p in enumerate(parts))
    mix = "".join(f"[s{i}]" for i in range(speakers))
    graph += f";{mix}amix=inputs={speakers}:normalize=0[m];[m]pan=stereo|c0=c0|c1=c0[out]"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-filter_complex", graph, "-map", "[out]",
         "-c:a", "aac", "-b:a", "128k", str(out)],
        check=True,
    )


def main() -> int:
    here = Path(__file__).parent / "set"
    here.mkdir(exist_ok=True)
    for name, seconds, speakers in SET:
        out = here / f"{name}.m4a"
        build(out, seconds, speakers)
        print(f"{out.name:16s} {seconds:5d}s {speakers} speakers  {out.stat().st_size/1e6:6.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
