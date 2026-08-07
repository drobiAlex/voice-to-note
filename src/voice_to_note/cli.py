import argparse
import sys
from pathlib import Path

from . import services
from .storage.repository import Repository
from .transforms.segments import display_name, fmt_ts


def cmd_process(args: argparse.Namespace) -> None:
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    repo = Repository()
    result = services.process_memo(repo, src, log=print)
    print(
        f"memo {result.memo_id} — {result.segment_count} segments,"
        f" {len(result.labels)} speakers, language={result.language}"
    )
    try:
        print("extracting notes …")
        backend = services.run_extraction(repo, result.memo_id)
        print(f"extracted via {backend}\n")
        print(services.notes(repo, result.memo_id))
    except services.ExtractionError as e:
        print(f"extraction skipped: {e}")
        print(f"retry later with: vtn extract {result.memo_id}")


def cmd_list(args: argparse.Namespace) -> None:
    memos = Repository().memos()
    if not memos:
        print("no memos yet")
        return
    for m in memos:
        dur = f"{m.duration_s:.0f}s" if m.duration_s else "?"
        print(f"{m.id:>4}  {m.created_at}  {dur:>6}  {m.language or '?':<3}"
              f"  {m.status:<12} {m.filename}")


def cmd_show(args: argparse.Namespace) -> None:
    repo = Repository()
    memo = services.require_memo(repo, args.id)
    names = repo.display_names(args.id)
    print(f"memo {memo.id} — {memo.filename} ({memo.status})\n")
    for s in repo.segments(args.id):
        print(f"{fmt_ts(s.t0_ms)}  {display_name(s.speaker, names)}: {s.text}")


def cmd_diarize(args: argparse.Namespace) -> None:
    labels = services.rediarize(Repository(), args.id, log=print)
    print(f"done — {len(labels)} speakers: {', '.join(labels)}")


def cmd_extract(args: argparse.Namespace) -> None:
    repo = Repository()
    services.require_memo(repo, args.id)
    print(f"extracting memo {args.id} …")
    backend = services.run_extraction(repo, args.id)
    print(f"done via {backend}\n")
    print(services.notes(repo, args.id))


def cmd_notes(args: argparse.Namespace) -> None:
    print(services.notes(Repository(), args.id))


def cmd_ask(args: argparse.Namespace) -> None:
    backend, answer = services.ask(Repository(), args.id, " ".join(args.question))
    print(f"({backend})\n\n{answer}")


def cmd_rename(args: argparse.Namespace) -> None:
    services.rename_speaker(Repository(), args.id, args.label, args.name)
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
    try:
        args.fn(args)
    except services.NotFound as e:
        sys.exit(str(e))
