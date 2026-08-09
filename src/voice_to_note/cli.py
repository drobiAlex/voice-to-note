import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import services
from .gateways import GatewayError
from .storage.repository import Repository

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
    with Repository() as repo:
        result = services.process_memo(repo, src, project=args.project, log=status)
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
    with Repository() as repo:
        if args.json:
            print(services.memos_json(repo, project=args.project))
            return
        listing = services.memos_text(repo, project=args.project)
        if listing:
            print(listing)
        else:
            status("no memos yet")


def cmd_move(args: argparse.Namespace) -> None:
    """Files a memo under a different project."""
    with Repository() as repo:
        services.move_memo(repo, args.id, args.project)
    status(f"memo {args.id} moved to {args.project}")


def cmd_show(args: argparse.Namespace) -> None:
    """Prints one memo's transcript."""
    with Repository() as repo:
        if args.json:
            # scripts get an error for an unknown id, never an empty transcript
            services.require_memo(repo, args.id)
            print(services.transcript_json(repo, args.id, raw=args.raw))
            return
        status(services.memo_heading(repo, args.id) + "\n")
        lines = services.transcript_lines(repo, args.id, raw=args.raw)
        if lines:
            print(lines)


def cmd_diarize(args: argparse.Namespace) -> None:
    """Redoes speaker detection when the first pass got voices wrong."""
    with Repository() as repo:
        labels = services.rediarize(repo, args.id, log=status)
    status(f"done — {len(labels)} speakers: {', '.join(labels)}")


def cmd_refine(args: argparse.Namespace) -> None:
    """Repairs transcription errors in a memo, or shows what it would repair."""
    with Repository() as repo:
        result = services.refine_transcript(repo, args.id, dry_run=args.diff)
        if args.diff:
            diff = services.refine_diff_text(result)
            if diff:
                print(diff)
            return
        status(
            f"memo {args.id}: repaired {len(result.changes)},"
            f" flagged {len(result.flagged)}, unchanged {result.untouched}"
        )


def cmd_extract(args: argparse.Namespace) -> None:
    """Redoes note extraction, for when it was skipped or came out poorly."""
    with Repository() as repo:
        services.require_memo(repo, args.id)
        status(f"extracting memo {args.id} …")
        backend = services.run_extraction(repo, args.id)
        status(f"done via {backend}\n")
        print(services.notes(repo, args.id))


def cmd_notes(args: argparse.Namespace) -> None:
    """Prints the notes already extracted for a memo."""
    with Repository() as repo:
        print(services.notes_json(repo, args.id) if args.json else services.notes(repo, args.id))


def cmd_ask(args: argparse.Namespace) -> None:
    """Answers a question about one memo."""
    with Repository() as repo:
        backend, answer = services.ask(repo, args.id, " ".join(args.question))
    status(f"({backend})\n")
    print(answer)


def cmd_rename(args: argparse.Namespace) -> None:
    """Puts a name to a speaker, which later memos then recognise by voice."""
    with Repository() as repo:
        services.rename_speaker(repo, args.id, args.label, args.name)
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
    sp.add_argument("--project", default="other", help="file the memo under a project")
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("list", help="list memos")
    sp.add_argument("--json", action="store_true", help="print memos as JSON")
    sp.add_argument("--project", help="only the memos filed under this project")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("move", help="file a memo under a project: move <id> <project>")
    sp.add_argument("id", type=int)
    sp.add_argument("project")
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("show", help="show a memo transcript")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true", help="print segments as JSON")
    sp.add_argument(
        "--raw", action="store_true", help="print the transcription before any repair"
    )
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("diarize", help="(re)run diarization on an existing memo")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_diarize)

    sp = sub.add_parser("refine", help="repair transcription errors in a memo")
    sp.add_argument("id", type=int)
    sp.add_argument("--diff", action="store_true", help="show the repairs without storing them")
    sp.set_defaults(fn=cmd_refine)

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
    except (
        services.NotFound,
        services.ExtractionError,
        services.InvalidInput,
        GatewayError,
    ) as e:
        # a missing binary, model or unreadable recording is something the user
        # can fix, so it gets one line; anything else is a bug and keeps its
        # traceback, which is the only thing that locates it
        sys.exit(str(e))
