import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config, services
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


def cmd_setup(args: argparse.Namespace) -> None:
    """Installs whisper.cpp and every model this app depends on: one line per
    finished step on stdout, and each download's progress overwriting itself
    on stderr so a multi-gigabyte fetch never looks stalled. --mock swaps in a
    world that touches neither the network nor disk, previewing the same flow
    in seconds."""
    fresh = {"line": False}

    def log(line: str) -> None:
        if fresh["line"]:
            print(file=sys.stderr)
            fresh["line"] = False
        print(line)

    def download(line: str) -> None:
        print("\r" + line, end="", file=sys.stderr, flush=True)
        fresh["line"] = True

    if args.mock:
        print(services.setup(log, download, world=services.mock_world()))
    else:
        print(services.setup(log, download))


def cmd_process(args: argparse.Namespace) -> None:
    """Takes a recording all the way to notes on screen. The note template is
    checked before the file is even touched: a typo must not survive minutes
    of converting, transcribing and diarizing only to fail at the last step."""
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    services.note_template(args.template)
    with Repository() as repo:
        result = services.process_memo(repo, src, project=args.project, log=status)
        status(
            f"memo {result.memo_id} — {result.segment_count} segments,"
            f" {len(result.labels)} speakers, language={result.language}"
        )
        try:
            status("extracting notes …")
            backend = services.run_extraction(repo, result.memo_id, template=args.template)
            status(f"extracted via {backend}\n")
            print(services.notes(repo, result.memo_id))
        except services.ExtractionError as e:
            status(f"extraction skipped: {e}")
            status(f"retry later with: vtn extract {result.memo_id}")


def cmd_list(args: argparse.Namespace) -> None:
    """Shows what has been processed so far."""
    with Repository() as repo:
        if args.json:
            print(services.memos_json(repo, project=args.project, tag=args.tag))
            return
        listing = services.memos_text(repo, project=args.project, tag=args.tag)
        if listing:
            print(listing)
        else:
            status("no memos yet")


def cmd_move(args: argparse.Namespace) -> None:
    """Files a memo under a different project."""
    with Repository() as repo:
        services.move_memo(repo, args.id, args.project)
    status(f"memo {args.id} moved to {args.project}")


def cmd_info(args: argparse.Namespace) -> None:
    """Prints what state one memo is in."""
    with Repository() as repo:
        print(services.memo_info_text(repo, args.id))


def _refiled(moved: int, project: str) -> str:
    """How much a bulk refiling carried and where it went, in plain English."""
    return f"{moved} memo{'' if moved == 1 else 's'} moved to {project}"


def cmd_project_rename(args: argparse.Namespace) -> None:
    """Renames a project, carrying every memo filed under it."""
    with Repository() as repo:
        moved = services.rename_project(repo, args.old, args.new)
    status(_refiled(moved, args.new))


def cmd_project_remove(args: argparse.Namespace) -> None:
    """Empties a project, which is the same thing as filing everything in it
    under other: nothing is deleted, so nothing here asks twice."""
    with Repository() as repo:
        moved = services.remove_project(repo, args.name)
    status(_refiled(moved, "other"))


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
        labels = services.rediarize(repo, args.id, log=status, num_speakers=args.speakers)
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
        backend = services.run_extraction(repo, args.id, force=args.force, template=args.template)
        status(f"done via {backend}\n")
        print(services.notes(repo, args.id))


def cmd_notes(args: argparse.Namespace) -> None:
    """Prints the notes already extracted for a memo, or the note as the screen
    shows it: whatever somebody wrote by hand, falling back to the extraction."""
    with Repository() as repo:
        if args.edited:
            print(services.notes_markdown(repo, args.id))
        else:
            print(services.notes_json(repo, args.id) if args.json else services.notes(repo, args.id))


def cmd_ask(args: argparse.Namespace) -> None:
    """Answers a question about one memo."""
    with Repository() as repo:
        backend, answer = services.ask(repo, args.id, " ".join(args.question))
    status(f"({backend})\n")
    print(answer)


def cmd_config(args: argparse.Namespace) -> None:
    """Lists every setting: the value in effect, where it came from, and what
    it does."""
    rows = services.config_rows()
    key_w = max(len(key) for key, _, _, _ in rows)
    value_w = max(len(value) for _, value, _, _ in rows)
    source_w = max(len(source) for _, _, source, _ in rows)
    for key, value, source, doc in rows:
        print(f"{key:<{key_w}}  {value:<{value_w}}  {source:<{source_w}}  {doc}")


def cmd_config_set(args: argparse.Namespace) -> None:
    """Writes one setting into vtn.toml."""
    status(services.config_set(args.key, args.value))


def cmd_config_unset(args: argparse.Namespace) -> None:
    """Clears one setting from vtn.toml back to its default."""
    status(services.config_unset(args.key))


def cmd_config_reset(args: argparse.Namespace) -> None:
    """Clears every setting from vtn.toml back to its default."""
    status(services.config_reset())


def cmd_template(args: argparse.Namespace) -> None:
    """Lists every prompt template: whether it is built-in or a saved
    override, and where an override file would go."""
    for name, state in services.template_rows():
        print(f"{name:<8} {state:<10} {config.TEMPLATES_DIR / f'{name}.md'}")
    status(f'\nwrite a custom one with: vtn template show <name> > "{config.TEMPLATES_DIR}/<name>.md"')


def cmd_template_show(args: argparse.Namespace) -> None:
    """Prints one template's effective text: a saved override if there is
    one, else the text this app ships with."""
    print(services.template_text(args.name))


def cmd_template_reset(args: argparse.Namespace) -> None:
    """Deletes a template's override file, restoring the built-in text."""
    status(services.template_reset(args.name))


def cmd_tui(args: argparse.Namespace) -> None:
    """Opens the memo browser."""
    # textual costs about as much to import as the whole rest of the app, and
    # every other command would pay it at the top of this file
    from .tui.app import MemoApp

    with Repository() as repo:
        MemoApp(repo).run()


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
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("process", help="transcribe an audio file")
    sp.add_argument("file")
    sp.add_argument("--project", default="other", help="file the memo under a project")
    sp.add_argument(
        "--template",
        default="notes",
        help="note template to extract with (see: vtn template)",
    )
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("list", help="list memos")
    sp.add_argument("--json", action="store_true", help="print memos as JSON")
    sp.add_argument("--project", help="only the memos filed under this project")
    sp.add_argument("--tag", help="only the memos whose notes carry this tag")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("move", help="file a memo under a project: move <id> <project>")
    sp.add_argument("id", type=int)
    sp.add_argument("project")
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("info", help="show what state a memo is in")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_info)

    # the one nested command in an otherwise flat set of verbs: a top-level
    # `rename` already names a speaker, and hyphenating would put two more verbs
    # in a list that is long enough to read through already
    sp = sub.add_parser("project", help="rename or empty a project")
    project_sub = sp.add_subparsers(dest="project_cmd", required=True)

    psp = project_sub.add_parser("rename", help="rename a project: project rename <old> <new>")
    psp.add_argument("old")
    psp.add_argument("new")
    psp.set_defaults(fn=cmd_project_rename)

    psp = project_sub.add_parser("remove", help="file a project's memos under other")
    psp.add_argument("name")
    psp.set_defaults(fn=cmd_project_remove)

    sp = sub.add_parser("show", help="show a memo transcript")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true", help="print segments as JSON")
    sp.add_argument(
        "--raw", action="store_true", help="print the transcription before any repair"
    )
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("diarize", help="(re)run diarization on an existing memo")
    sp.add_argument("id", type=int)
    sp.add_argument(
        "--speakers",
        type=int,
        default=None,
        metavar="N",
        help="pin the number of speakers instead of auto-detecting",
    )
    sp.set_defaults(fn=cmd_diarize)

    sp = sub.add_parser("refine", help="repair transcription errors in a memo")
    sp.add_argument("id", type=int)
    sp.add_argument("--diff", action="store_true", help="show the repairs without storing them")
    sp.set_defaults(fn=cmd_refine)

    sp = sub.add_parser("extract", help="(re)extract structured notes for a memo")
    sp.add_argument("id", type=int)
    sp.add_argument(
        "--force", action="store_true", help="replace notes you have edited by hand"
    )
    sp.add_argument(
        "--template",
        default="notes",
        help="note template to extract with (see: vtn template)",
    )
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("notes", help="show extracted notes for a memo")
    sp.add_argument("id", type=int)
    # an edit replaces the extraction --json prints, so there is no printing both
    form = sp.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true", help="print the stored extraction as JSON")
    form.add_argument(
        "--edited", action="store_true", help="print the note as the screen shows it"
    )
    sp.set_defaults(fn=cmd_notes)

    sp = sub.add_parser("ask", help="ask a question about a memo: ask <id> <question...>")
    sp.add_argument("id", type=int)
    sp.add_argument("question", nargs="+")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("setup", help="install whisper.cpp and all models (idempotent)")
    sp.add_argument(
        "--mock", action="store_true", help="preview the install flow without installing anything"
    )
    sp.set_defaults(fn=cmd_setup)

    sp = sub.add_parser(
        "config", help="list settings, or write one: config set|unset|reset"
    )
    sp.set_defaults(fn=cmd_config)
    config_sub = sp.add_subparsers(dest="config_cmd")

    csp = config_sub.add_parser("set", help="set a setting: config set <key> <value>")
    csp.add_argument("key")
    csp.add_argument("value")
    csp.set_defaults(fn=cmd_config_set)

    csp = config_sub.add_parser("unset", help="clear a setting: config unset <key>")
    csp.add_argument("key")
    csp.set_defaults(fn=cmd_config_unset)

    config_sub.add_parser(
        "reset", help="clear every setting back to its default"
    ).set_defaults(fn=cmd_config_reset)

    sp = sub.add_parser(
        "template", help="list, show or reset the built-in LLM prompts: template show|reset"
    )
    sp.set_defaults(fn=cmd_template)
    template_sub = sp.add_subparsers(dest="template_cmd")

    tsp = template_sub.add_parser("show", help="print a template's effective text: template show <name>")
    tsp.add_argument("name")
    tsp.set_defaults(fn=cmd_template_show)

    tsp = template_sub.add_parser(
        "reset", help="restore a template to its built-in text: template reset <name>"
    )
    tsp.add_argument("name")
    tsp.set_defaults(fn=cmd_template_reset)

    sub.add_parser(
        "tui", help="browse, edit and process memos on one screen"
    ).set_defaults(fn=cmd_tui)

    sp = sub.add_parser("rename", help="name a speaker: rename <memo_id> <label> <name>")
    sp.add_argument("id", type=int)
    sp.add_argument("label")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_rename)

    args = p.parse_args()
    if args.cmd is None:
        # no subcommand at all, as opposed to one argparse already rejected
        if not services.ready():
            sys.exit("not set up yet — run: vtn setup (installs whisper.cpp and all models)")
        args.fn = cmd_tui
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
