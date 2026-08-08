import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import services
from .gateways import GatewayError
from .storage.repository import Repository
from .transforms.segments import display_name, fmt_ts

EXAMPLES = """examples:
  vtn process ~/memos/standup.m4a     transcribe, diarize, then extract notes
  vtn show 3                          print memo 3's transcript
  vtn notes 3 --json > notes.json     machine-readable notes for scripting
  vtn rename 3 S1 Samantha            name a speaker; later memos match by voice
"""


def status(message: str = "") -> None:
    """Progress and confirmations, kept off stdout so output stays pipeable."""
    print(message, file=sys.stderr)


def _version() -> str:
    """The installed version, for bug reports."""
    try:
        return version("voice-to-note")
    except PackageNotFoundError:  # running straight from a source checkout
        return "unknown"


def cmd_process(args: argparse.Namespace) -> None:
    """Takes a recording all the way to notes on screen."""
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    repo = Repository()
    result = services.process_memo(repo, src, log=status)
    status(
        f"memo {result.memo_id} — {result.segment_count} segments,"
        f" {len(result.labels)} speakers, language={result.language}"
    )
    try:
        status("extracting notes …")
        backend = services.run_extraction(repo, result.memo_id)
        status(f"extracted via {backend}\n")
        print(services.notes(repo, result.memo_id))
    except services.ExtractionError as e:
        status(f"extraction skipped: {e}")
        status(f"retry later with: vtn extract {result.memo_id}")


def cmd_list(args: argparse.Namespace) -> None:
    """Shows what has been processed so far."""
    repo = Repository()
    if args.json:
        print(services.memos_json(repo))
        return
    memos = repo.memos()
    if not memos:
        status("no memos yet")
        return
    for m in memos:
        dur = f"{m.duration_s:.0f}s" if m.duration_s else "?"
        print(f"{m.id:>4}  {m.created_at}  {dur:>6}  {m.language or '?':<3}"
              f"  {m.status:<12} {m.filename}")


def cmd_show(args: argparse.Namespace) -> None:
    """Prints one memo's transcript."""
    repo = Repository()
    memo = services.require_memo(repo, args.id)
    if args.json:
        print(services.transcript_json(repo, args.id))
        return
    names = repo.display_names(args.id)
    status(f"memo {memo.id} — {memo.filename} ({memo.status})\n")
    for s in repo.segments(args.id):
        print(f"{fmt_ts(s.t0_ms)}  {display_name(s.speaker, names)}: {s.text}")


def cmd_diarize(args: argparse.Namespace) -> None:
    """Redoes speaker detection when the first pass got voices wrong."""
    labels = services.rediarize(Repository(), args.id, log=status)
    status(f"done — {len(labels)} speakers: {', '.join(labels)}")


def cmd_extract(args: argparse.Namespace) -> None:
    """Redoes note extraction, for when it was skipped or came out poorly."""
    repo = Repository()
    services.require_memo(repo, args.id)
    status(f"extracting memo {args.id} …")
    backend = services.run_extraction(repo, args.id)
    status(f"done via {backend}\n")
    print(services.notes(repo, args.id))


def cmd_notes(args: argparse.Namespace) -> None:
    """Prints the notes already extracted for a memo."""
    repo = Repository()
    print(services.notes_json(repo, args.id) if args.json else services.notes(repo, args.id))


def cmd_ask(args: argparse.Namespace) -> None:
    """Answers a question about one memo."""
    backend, answer = services.ask(Repository(), args.id, " ".join(args.question))
    status(f"({backend})\n")
    print(answer)


def cmd_rename(args: argparse.Namespace) -> None:
    """Puts a name to a speaker, which later memos then recognise by voice."""
    services.rename_speaker(Repository(), args.id, args.label, args.name)
    status(f"memo {args.id}: {args.label} -> {args.name}")


def main() -> None:
    """The vtn command."""
    p = argparse.ArgumentParser(
        prog="vtn",
        description="voice-to-note",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"vtn {_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("process", help="transcribe an audio file")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("list", help="list memos")
    sp.add_argument("--json", action="store_true", help="print memos as JSON")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="show a memo transcript")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true", help="print segments as JSON")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("diarize", help="(re)run diarization on an existing memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_diarize)

    sp = sub.add_parser("extract", help="(re)extract structured notes for a memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("notes", help="show extracted notes for a memo")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true", help="print the stored extraction as JSON")
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
    except (services.NotFound, services.ExtractionError, GatewayError) as e:
        # a missing binary, model or unreadable recording is something the user
        # can fix, so it gets one line; anything else is a bug and keeps its
        # traceback, which is the only thing that locates it
        sys.exit(str(e))
