import argparse
import sys
import uuid
from pathlib import Path

from . import audio, config, db, diarize, transcribe


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
    print("diarizing …")
    turns = diarize.diarize(wav)
    diarize.assign_speakers(segs, turns)
    labels = sorted({s["speaker"] for s in segs if s["speaker"]})
    con = db.connect()
    with con:
        cur = con.execute(
            "INSERT INTO memos (filename, wav_path, duration_s, language, status)"
            " VALUES (?,?,?,?,'transcribed')",
            (src.name, str(wav), dur, lang),
        )
        memo_id = cur.lastrowid
        con.executemany(
            "INSERT INTO segments (memo_id, t0_ms, t1_ms, text, speaker) VALUES (?,?,?,?,?)",
            [(memo_id, s["t0_ms"], s["t1_ms"], s["text"], s["speaker"]) for s in segs],
        )
        con.executemany(
            "INSERT INTO speakers (memo_id, label) VALUES (?,?)",
            [(memo_id, lb) for lb in labels],
        )
    print(f"memo {memo_id} — {len(segs)} segments, {len(labels)} speakers, language={lang}\n")
    for s in segs:
        print(f"{fmt_ts(s['t0_ms'])}  {s['speaker'] or '?'}: {s['text']}")


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


def speaker_names(con, memo_id: int) -> dict:
    return {
        r["label"]: r["name"] or r["label"]
        for r in con.execute("SELECT label, name FROM speakers WHERE memo_id=?", (memo_id,))
    }


def cmd_show(args: argparse.Namespace) -> None:
    con = db.connect()
    memo = con.execute("SELECT * FROM memos WHERE id=?", (args.id,)).fetchone()
    if not memo:
        sys.exit(f"no memo with id {args.id}")
    names = speaker_names(con, args.id)
    print(f"memo {memo['id']} — {memo['filename']} ({memo['status']})\n")
    for s in con.execute(
        "SELECT * FROM segments WHERE memo_id=? ORDER BY t0_ms", (args.id,)
    ):
        who = f"{names.get(s['speaker'], s['speaker'])}: " if s["speaker"] else ""
        print(f"{fmt_ts(s['t0_ms'])}  {who}{s['text']}")


def cmd_diarize(args: argparse.Namespace) -> None:
    con = db.connect()
    memo = con.execute("SELECT * FROM memos WHERE id=?", (args.id,)).fetchone()
    if not memo:
        sys.exit(f"no memo with id {args.id}")
    wav = Path(memo["wav_path"])
    if not wav.exists():
        sys.exit(f"wav missing: {wav}")
    segs = [
        dict(r)
        for r in con.execute(
            "SELECT id, t0_ms, t1_ms FROM segments WHERE memo_id=? ORDER BY t0_ms",
            (args.id,),
        )
    ]
    print(f"diarizing memo {args.id} …")
    turns = diarize.diarize(wav)
    diarize.assign_speakers(segs, turns)
    labels = sorted({s["speaker"] for s in segs if s["speaker"]})
    with con:
        con.executemany(
            "UPDATE segments SET speaker=? WHERE id=?",
            [(s["speaker"], s["id"]) for s in segs],
        )
        con.execute("DELETE FROM speakers WHERE memo_id=?", (args.id,))
        con.executemany(
            "INSERT INTO speakers (memo_id, label) VALUES (?,?)",
            [(args.id, lb) for lb in labels],
        )
    print(f"done — {len(labels)} speakers: {', '.join(labels)}")


def cmd_rename(args: argparse.Namespace) -> None:
    con = db.connect()
    with con:
        cur = con.execute(
            "UPDATE speakers SET name=? WHERE memo_id=? AND label=?",
            (args.name, args.id, args.label),
        )
    if cur.rowcount == 0:
        sys.exit(f"no speaker {args.label} in memo {args.id}")
    print(f"memo {args.id}: {args.label} -> {args.name}")


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

    sp = sub.add_parser("diarize", help="(re)run diarization on an existing memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_diarize)

    sp = sub.add_parser("rename", help="name a speaker: rename <memo_id> <label> <name>")
    sp.add_argument("id", type=int)
    sp.add_argument("label")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_rename)

    args = p.parse_args()
    args.fn(args)
