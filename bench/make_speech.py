"""Builds speech fixtures for the stages that need real words in them.

The shaped noise in `make_set.py` is enough to time conversion and the writes,
but not transcription: whisper with `--vad` skips silence and finds no speech
in noise, so a run over it reports a speed the real thing never reaches. This
takes the sample sentence that ships with whisper.cpp and repeats it, pitch-
shifted per speaker and gated so that only one voice holds the floor at a time
— the same gate `make_set.py` uses, because diarization in practice sees turns,
not five copies of a sentence summed on top of each other, and because summing
full-scale copies would clip the mix.

Also writes `speech-3.m4a`, the fixture `bench/pipeline.py` reads: the real
`process_memo` starts from what a phone hands it — 48 kHz stereo m4a — so the
conversion stage is measured on the real thing.

Needs `vtn setup` to have cloned whisper.cpp. Run after make_set.py.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_to_note import config  # noqa: E402

SET = [("speech-1", 30, 1), ("speech-3", 480, 3), ("speech-5", 1500, 5)]
# a rate per speaker: resampling and then correcting the tempo moves the voice
# without changing how fast it talks
RATES = (1.0, 1.15, 0.88, 1.28, 0.8)
TURN_S = 6          # how long each voice holds the floor


def voice(index: int, speakers: int) -> str:
    """One speaker's version of the sample sentence, silent off its own turns."""
    rate = RATES[index % len(RATES)]
    turn = f"eq(mod(floor(t/{TURN_S})\\,{speakers})\\,{index})"
    gate = f"volume='if({turn}\\,1\\,0)':eval=frame"
    if rate == 1.0:
        return f"[{index}:a]{gate}[v{index}]"
    return (f"[{index}:a]asetrate=16000*{rate},aresample=16000,"
            f"atempo={1 / rate:.4f},{gate}[v{index}]")


def build(sample: Path, out: Path, seconds: int, speakers: int) -> None:
    """A recording of the voices taking turns, cut to length."""
    inputs: list[str] = []
    for _ in range(speakers):
        inputs += ["-stream_loop", "-1", "-i", str(sample)]
    graph = ";".join(voice(i, speakers) for i in range(speakers))
    turns = "".join(f"[v{i}]" for i in range(speakers))
    graph += f";{turns}amix=inputs={speakers}:normalize=0[m]" if speakers > 1 else ";[v0]anull[m]"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", graph, "-map", "[m]", "-t", str(seconds),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
        check=True,
    )


def to_m4a(wav: Path, out: Path) -> None:
    """The pipeline fixture in the shape a phone hands `vtn process`."""
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
         "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(out)],
        check=True,
    )


def main() -> int:
    # resolved through config rather than a cwd-relative literal: vendor lives
    # wherever VTN_HOME (or the default application-support home) put it, and
    # "run vtn setup first" is only honest advice when this looks where setup
    # actually wrote
    sample = config.VENDOR / "samples" / "jfk.wav"
    if not sample.exists():
        print(f"missing {sample} — run vtn setup first "
              "(or ./run.sh, with VTN_HOME=$PWD matching this run)")
        return 1
    out_dir = Path(__file__).parent / "set"
    out_dir.mkdir(exist_ok=True)
    for name, seconds, speakers in SET:
        out = out_dir / f"{name}.wav"
        build(sample, out, seconds, speakers)
        print(f"{out.name:14s} {seconds:5d}s {speakers} voices  {out.stat().st_size/1e6:6.1f} MB")
    m4a = out_dir / "speech-3.m4a"
    to_m4a(out_dir / "speech-3.wav", m4a)
    print(f"{m4a.name:14s} {'':5s}  for pipeline.py  {m4a.stat().st_size/1e6:6.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
