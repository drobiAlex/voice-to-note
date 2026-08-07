import argparse
import json
import sys
import uuid
from pathlib import Path

from . import audio, config, db, diarize, extract, transcribe


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
        labels = store_speakers(con, memo_id, wav, turns)
    print(f"memo {memo_id} — {len(segs)} segments, {len(labels)} speakers, language={lang}")
    try:
        print("extracting notes …")
        backend = run_extraction(con, memo_id)
        print(f"extracted via {backend}\n")
        print_notes(con, memo_id)
    except RuntimeError as e:
        print(f"extraction skipped: {e}")
        print(f"retry later with: vtn extract {memo_id}")


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
    keep_names = {
        r["label"]: r["name"]
        for r in con.execute(
            "SELECT label, name FROM speakers WHERE memo_id=? AND name IS NOT NULL",
            (args.id,),
        )
    }
    print(f"diarizing memo {args.id} …")
    turns = diarize.diarize(wav)
    diarize.assign_speakers(segs, turns)
    with con:
        con.executemany(
            "UPDATE segments SET speaker=? WHERE id=?",
            [(s["speaker"], s["id"]) for s in segs],
        )
        labels = store_speakers(con, args.id, wav, turns, keep_names)
    print(f"done — {len(labels)} speakers: {', '.join(labels)}")


def store_speakers(con, memo_id: int, wav: Path, turns: list[dict],
                   keep_names: dict | None = None) -> list[str]:
    keep_names = keep_names or {}
    embs = diarize.speaker_embeddings(wav, turns)
    con.execute("DELETE FROM speakers WHERE memo_id=?", (memo_id,))
    matches = diarize.match_known_speakers(con, embs)
    labels = sorted({t["speaker"] for t in turns})
    rows = []
    for lb in labels:
        name = keep_names.get(lb) or (matches[lb][0] if lb in matches else None)
        emb = embs.get(lb)
        rows.append((memo_id, lb, name, emb.tobytes() if emb is not None else None))
    con.executemany(
        "INSERT INTO speakers (memo_id, label, name, embedding) VALUES (?,?,?,?)",
        rows,
    )
    for lb, (nm, sim) in matches.items():
        if not keep_names.get(lb):
            print(f"  {lb} sounds like {nm} (similarity {sim:.2f}) — auto-named")
    return labels


def transcript_text(con, memo_id: int) -> str:
    names = speaker_names(con, memo_id)
    lines: list[str] = []
    cur_sp, cur_t0, buf = None, 0, []
    for s in con.execute(
        "SELECT t0_ms, speaker, text FROM segments WHERE memo_id=? ORDER BY t0_ms",
        (memo_id,),
    ):
        sp = names.get(s["speaker"], s["speaker"]) or "Unknown"
        if sp != cur_sp:
            if buf:
                lines.append(f"[{fmt_ts(cur_t0)}] {cur_sp}: {' '.join(buf)}")
            cur_sp, cur_t0, buf = sp, s["t0_ms"], []
        buf.append(s["text"])
    if buf:
        lines.append(f"[{fmt_ts(cur_t0)}] {cur_sp}: {' '.join(buf)}")
    return "\n".join(lines)


def run_extraction(con, memo_id: int) -> str:
    backend, data = extract.extract(transcript_text(con, memo_id))
    with con:
        con.execute("DELETE FROM extractions WHERE memo_id=?", (memo_id,))
        con.execute(
            "INSERT INTO extractions (memo_id, backend, json) VALUES (?,?,?)",
            (memo_id, backend, json.dumps(data, ensure_ascii=False)),
        )
        con.execute("UPDATE memos SET status='extracted' WHERE id=?", (memo_id,))
    return backend


def print_notes(con, memo_id: int) -> None:
    row = con.execute(
        "SELECT backend, json, created_at FROM extractions WHERE memo_id=?", (memo_id,)
    ).fetchone()
    if not row:
        sys.exit(f"no extraction for memo {memo_id} — run: vtn extract {memo_id}")
    d = json.loads(row["json"])
    print(f"# {d['title']}")
    print(f"  ({row['backend']}, {row['created_at']})\n")
    print(d["summary"] + "\n")
    if d["action_items"]:
        print("ACTION ITEMS")
        for a in d["action_items"]:
            extra = ", ".join(x for x in (a.get("owner"), a.get("deadline")) if x)
            print(f"  [ ] {a['task']}" + (f"  ({extra})" if extra else ""))
        print()
    for key, header in (
        ("decisions", "DECISIONS"),
        ("key_insights", "KEY INSIGHTS"),
        ("open_questions", "OPEN QUESTIONS"),
    ):
        if d[key]:
            print(header)
            for item in d[key]:
                print(f"  - {item}")
            print()
    if d["dates"]:
        print("DATES")
        for x in d["dates"]:
            print(f"  - {x['date']}: {x['context']}")
        print()
    if d["tags"]:
        print("tags: " + ", ".join(d["tags"]))


def cmd_extract(args: argparse.Namespace) -> None:
    con = db.connect()
    if not con.execute("SELECT 1 FROM memos WHERE id=?", (args.id,)).fetchone():
        sys.exit(f"no memo with id {args.id}")
    print(f"extracting memo {args.id} …")
    backend = run_extraction(con, args.id)
    print(f"done via {backend}\n")
    print_notes(con, args.id)


def cmd_notes(args: argparse.Namespace) -> None:
    print_notes(db.connect(), args.id)


def cmd_ask(args: argparse.Namespace) -> None:
    con = db.connect()
    if not con.execute("SELECT 1 FROM memos WHERE id=?", (args.id,)).fetchone():
        sys.exit(f"no memo with id {args.id}")
    question = " ".join(args.question)
    backend, answer = extract.ask(transcript_text(con, args.id), question)
    print(f"({backend})\n\n{answer}")


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

    sp = sub.add_parser("extract", help="(re)extract structured notes for a memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("notes", help="show extracted notes for a memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_notes)

    sp = sub.add_parser("ask", help="ask a question about a memo: ask <id> <question...>")
    sp.add_argument("id", type=int)
    sp.add_argument("question", nargs="+")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("rename", help="name a speaker: rename <memo_id> <label> <name>")
    sp.add_argument("id", type=int)
    sp.add_argument("label")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_rename)

    args = p.parse_args()
    args.fn(args)
