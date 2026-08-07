import argparse
import sys
import uuid
from pathlib import Path

from . import audio, config, db, transcribe


def fmt_ts(ms: int) -> str:
    m, s = divmod(ms // 1000, 60)
    return f"{m:02d}:{s:02d}"


def cmd_process(args: argparse.Namespace) -> None:
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    wav = config.UPLOADS_DIR / f"{src.stem}-{uuid.uuid4().hex[:8]}.wav"
    print(f"converting {src.name} …")
    audio.to_wav16k(src, wav)
    dur = audio.duration_seconds(wav)
    print(f"transcribing ({dur:.0f}s audio) …")
    raw = transcribe.transcribe(wav)
    segs = transcribe.segments(raw)
    lang = raw.get("result", {}).get("language", "")
    con = db.connect()
    with con:
        cur = con.execute(
            "INSERT INTO memos (filename, wav_path, duration_s, language, status)"
            " VALUES (?,?,?,?,'transcribed')",
            (src.name, str(wav), dur, lang),
        )
        memo_id = cur.lastrowid
        con.executemany(
            "INSERT INTO segments (memo_id, t0_ms, t1_ms, text) VALUES (?,?,?,?)",
            [(memo_id, s["t0_ms"], s["t1_ms"], s["text"]) for s in segs],
        )
    print(f"memo {memo_id} — {len(segs)} segments, language={lang}\n")
    for s in segs:
        print(f"{fmt_ts(s['t0_ms'])}  {s['text']}")


def cmd_list(args: argparse.Namespace) -> None:
    con = db.connect()
    rows = con.execute(
        "SELECT id, filename, duration_s, language, status, created_at"
        " FROM memos ORDER BY id DESC"
    ).fetchall()
    if not rows:
        print("no memos yet")
        return
    for r in rows:
        dur = f"{r['duration_s']:.0f}s" if r["duration_s"] else "?"
        print(f"{r['id']:>4}  {r['created_at']}  {dur:>6}  {r['language'] or '?':<3}"
              f"  {r['status']:<12} {r['filename']}")


def cmd_show(args: argparse.Namespace) -> None:
    con = db.connect()
    memo = con.execute("SELECT * FROM memos WHERE id=?", (args.id,)).fetchone()
    if not memo:
        sys.exit(f"no memo with id {args.id}")
    print(f"memo {memo['id']} — {memo['filename']} ({memo['status']})\n")
    for s in con.execute(
        "SELECT * FROM segments WHERE memo_id=? ORDER BY t0_ms", (args.id,)
    ):
        who = f"{s['speaker']}: " if s["speaker"] else ""
        print(f"{fmt_ts(s['t0_ms'])}  {who}{s['text']}")


def main() -> None:
    p = argparse.ArgumentParser(prog="vtn", description="voice-to-note")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("process", help="transcribe an audio file")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("list", help="list memos")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="show a memo transcript")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_show)

    args = p.parse_args()
    args.fn(args)
