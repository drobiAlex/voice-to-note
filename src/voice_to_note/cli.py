import argparse
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config, services
from .gateways import GatewayError, audio, capture
from .storage.repository import Repository

EXAMPLES = """examples:
  vtn process ~/memos/standup.m4a     transcribe, diarize, then extract notes
  vtn record --project work           tape this Mac's meeting, then process it
  vtn show 3                          print memo 3's transcript
  vtn notes 3 --json > notes.json     machine-readable notes for scripting
  vtn rename 3 S1 Samantha            name a speaker; later memos match by voice
  vtn title 3 Sprint planning         give memo 3 a name of its own
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


def _checked(args: argparse.Namespace) -> tuple[frozenset[str], int | None]:
    """The options that decide what the pipeline does, all read before any
    audio is: a typo in a template, a step or a speaker count must not survive
    minutes of converting, transcribing and diarizing — or a whole meeting —
    only to fail at the last step."""
    services.note_template(args.template)
    return services.process_steps(args.steps), services.speaker_count(args.speakers)


def _pipeline(
    repo: Repository,
    src: Path,
    args: argparse.Namespace,
    steps: frozenset[str],
    count: int | None,
) -> None:
    """Everything that happens to a recording once it exists and its options
    have been checked. Shared, so that a file handed in and a meeting taped a
    moment ago travel exactly the same path. The database comes in already open:
    the caller may have had to ask it something before any of this could start,
    and one connection for the command keeps that question and this work reading
    the same memos."""
    result = services.process_memo(
        repo,
        src,
        project=args.project,
        log=status,
        num_speakers=count,
        diarize="speakers" in steps,
    )
    status(
        f"memo {result.memo_id} — {result.segment_count} segments,"
        f" {len(result.labels)} speakers, language={result.language}"
    )
    if "refine" in steps:
        try:
            status("refining transcript …")
            refined = services.refine_transcript(repo, result.memo_id)
            status(
                f"refined — {len(refined.changes)} repaired,"
                f" {len(refined.flagged)} flagged"
            )
        except services.ExtractionError as e:
            status(f"refine skipped: {e}")
    if "notes" in steps:
        try:
            status("extracting notes …")
            backend = services.run_extraction(repo, result.memo_id, template=args.template)
            status(f"extracted via {backend}\n")
            print(services.notes(repo, result.memo_id))
        except services.ExtractionError as e:
            status(f"extraction skipped: {e}")
            status(f"retry later with: vtn extract {result.memo_id}")
    else:
        status(f"transcript stored — run `vtn extract {result.memo_id}` for notes")


def cmd_process(args: argparse.Namespace) -> None:
    """Takes a recording all the way to notes on screen, asking first when this
    same recording has already been through here. Asked before the work rather
    than after it: a second copy of a memo costs minutes of transcription and
    then sits in the list looking like a separate meeting, and only the person
    at the keyboard knows whether a file taped at the same moment as one already
    stored is the same recording or a deliberate second pass over it."""
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"no such file: {src}")
    steps, count = _checked(args)
    with Repository() as repo:
        stored = services.find_duplicate(repo, src)
        if stored is not None and not _confirmed(
            f"memo {stored.id} — {stored.filename} was recorded at the same"
            " moment; process this file anyway?"
        ):
            status("skipped")
            return
        _pipeline(repo, src, args, steps, count)


def _taped(track: Path) -> bool:
    """Whether a track holds anything at all. A helper that fell over before it
    opened its files leaves nothing worth merging behind."""
    return track.exists() and track.stat().st_size > 0


def _meter(raw: bool) -> tuple[Callable[[float, float], None] | None, Callable[[], None]]:
    """How loud each side is while the meeting runs, and how to leave the
    screen tidy afterwards. A terminal gets bars redrawn over themselves, so a
    muted microphone or a silent system tap is visible while it can still be
    fixed rather than an hour later in the transcript; --levels gets the raw
    numbers instead, for whatever wraps this command and draws its own meter.
    Output going anywhere but a terminal gets nothing at all, and nothing is
    then measured either — a meter nobody can see is not worth a pass over
    every buffer of a meeting.

    Anything redrawn in place has to be ended by a newline of its own before
    the next line is printed, or that line lands on top of the bars and reads
    as part of them; the caller does that by calling the second half of this
    once the recording is over."""
    fresh = {"line": False}

    def done() -> None:
        if fresh["line"]:
            print(file=sys.stderr)
            fresh["line"] = False

    def numbers(system_db: float, mic_db: float) -> None:
        print(f"level\t{system_db:.1f}\t{mic_db:.1f}", file=sys.stderr, flush=True)

    def bars(system_db: float, mic_db: float) -> None:
        print(
            "\r" + services.meter_line(system_db, mic_db), end="", file=sys.stderr, flush=True
        )
        fresh["line"] = True

    if raw:
        return numbers, done
    if sys.stderr.isatty():
        return bars, done
    return None, done


def cmd_record(args: argparse.Namespace) -> None:
    """Tapes a meeting on this Mac and takes it straight through to notes. The
    two sides — what the Mac plays and what the microphone hears — are recorded
    apart and then merged, because everything downstream reads a single
    recording. Once the merged file exists the raw tracks are dropped; until
    then they are kept whatever goes wrong, since they are the only copy of a
    meeting nobody can hold twice. A meter runs the whole time on a terminal,
    because a side that is recording silence is worth knowing about while
    somebody is still in the room to fix it."""
    if sys.platform != "darwin":
        sys.exit("meeting recording is macOS-only")
    steps, count = _checked(args)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tracks = config.RECORDINGS_DIR / stamp
    system_wav, mic_wav = tracks / "system.wav", tracks / "mic.wav"
    merged = config.RECORDINGS_DIR / f"meeting-{stamp}.m4a"

    levels, meter_done = _meter(args.levels)
    recording = capture.start(
        system_wav,
        mic_wav,
        output_uid=args.output_device,
        input_uid=args.input_device,
        levels=levels,
    )
    status(f"recording — press Ctrl+C to stop ({tracks})")
    try:
        code = recording.wait()
        ended = f"recording ended on its own: vtn-capture exited with code {code}"
    except KeyboardInterrupt:
        ended = ""
    # the helper is stopped before the meter is ended and anything else said: a
    # reading arriving afterwards would draw half a frame under the line below
    recording.stop()
    meter_done()
    status(ended)

    if not _taped(system_wav) or not _taped(mic_wav):
        sys.exit(f"nothing was recorded — no audio in {tracks}")
    status("merging the two tracks …")
    audio.merge_tracks(system_wav, mic_wav, merged)
    system_wav.unlink()
    mic_wav.unlink()
    tracks.rmdir()
    status(f"recorded {merged}")
    with Repository() as repo:
        _pipeline(repo, merged, args, steps, count)


def cmd_menubar(args: argparse.Namespace) -> None:
    """Opens the menu bar recorder, which starts and stops the same recording
    this command does — from the menu bar, so nothing has to keep a terminal
    open for the length of a meeting. Handed to the Finder rather than run from
    here: opened as an app it outlives this process, and macOS attributes the
    microphone and system-audio permissions to the bundle rather than to
    whatever launched it."""
    if sys.platform != "darwin":
        sys.exit("the menu bar recorder is macOS-only")
    if not config.MENUBAR_BIN.exists():
        sys.exit("menu bar recorder not built — run: vtn setup")
    subprocess.run(["open", str(config.MENUBAR_APP)], check=False)


def cmd_devices(args: argparse.Namespace) -> None:
    """Lists the audio devices a recording can be pointed at, so the UID that
    `record --output-device` and `--input-device` want can be copied from
    somewhere rather than guessed at. Printed as it comes: the helper already
    formats one device per line."""
    if sys.platform != "darwin":
        sys.exit("audio devices are listed on macOS only")
    print(capture.devices(), end="")


def cmd_projects(args: argparse.Namespace) -> None:
    """Lists every project and how many memos are filed under it, one per line
    and tab-separated, so the same list serves a person reading it and anything
    that has to offer the projects as a choice."""
    with Repository() as repo:
        for name, count in services.projects(repo):
            print(f"{name}\t{count}")


def cmd_list(args: argparse.Namespace) -> None:
    """Shows what has been processed so far."""
    with Repository() as repo:
        if args.json:
            print(
                services.memos_json(
                    repo, project=args.project, tag=args.tag, sort=args.sort
                )
            )
            return
        listing = services.memos_text(
            repo, project=args.project, tag=args.tag, sort=args.sort
        )
        if listing:
            print(listing)
        else:
            status("no memos yet")


def cmd_todos(args: argparse.Namespace) -> None:
    """Shows what the memos have committed to and nobody has done yet."""
    with Repository() as repo:
        listing = services.todos_text(
            repo,
            project=args.project,
            include_done=args.all,
            mine=args.mine,
            unassigned=args.unassigned,
        )
        if listing:
            print(listing)
        else:
            status("no to-dos")


def cmd_todo_done(args: argparse.Namespace) -> None:
    """Checks a to-do off."""
    with Repository() as repo:
        services.set_todo_status(repo, args.id, "done")
    status(f"to-do {args.id} done")


def cmd_todo_open(args: argparse.Namespace) -> None:
    """Puts a to-do back on the list, for one checked off too soon."""
    with Repository() as repo:
        services.set_todo_status(repo, args.id, "open")
    status(f"to-do {args.id} back on the list")


def cmd_move(args: argparse.Namespace) -> None:
    """Files a memo under a different project."""
    with Repository() as repo:
        services.move_memo(repo, args.id, args.project)
    status(f"memo {args.id} moved to {args.project}")


def _confirmed(question: str) -> bool:
    """Whether the user actually said yes, asked on stderr so the question does
    not land in a pipe. Anything but yes is a no, and so is a prompt nobody was
    there to answer: piping into a command that then waits on an answer it will
    never get must not end in the deletion nobody asked for."""
    print(f"{question} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_delete(args: argparse.Namespace) -> None:
    """Throws a memo away for good, asking first unless already told to."""
    with Repository() as repo:
        if not args.yes:
            memo = services.require_memo(repo, args.id)
            if not _confirmed(f"delete memo {args.id} — {memo.filename}?"):
                status("kept")
                return
        filename = services.delete_memo(repo, args.id)
    status(f"deleted memo {args.id} — {filename}")


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


def cmd_title(args: argparse.Namespace) -> None:
    """Gives a memo a name of its own, in place of the recording's file name."""
    name = " ".join(args.name)
    with Repository() as repo:
        services.rename_memo(repo, args.id, name)
    status(f"memo {args.id}: renamed to {name}")


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
    sp.add_argument(
        "--speakers",
        default="auto",
        metavar="N",
        help='how many voices are on the recording; "auto" guesses',
    )
    sp.add_argument(
        "--steps",
        default="speakers,notes",
        metavar="STEPS",
        help='comma-separated optional stages to run: speakers,refine,notes'
        ' (default: speakers,notes; "" imports just the transcript)',
    )
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("record", help="record this Mac's meeting, then transcribe it")
    sp.add_argument("--project", default="other", help="file the memo under a project")
    sp.add_argument(
        "--template",
        default="notes",
        help="note template to extract with (see: vtn template)",
    )
    sp.add_argument(
        "--speakers",
        default="auto",
        metavar="N",
        help='how many voices are in the meeting; "auto" guesses',
    )
    sp.add_argument(
        "--steps",
        default="speakers,notes",
        metavar="STEPS",
        help='comma-separated optional stages to run: speakers,refine,notes'
        ' (default: speakers,notes; "" imports just the transcript)',
    )
    sp.add_argument(
        "--output-device",
        metavar="UID",
        help="record this output device's playback instead of everything the Mac plays"
        " (see: vtn devices)",
    )
    sp.add_argument(
        "--input-device",
        metavar="UID",
        help="record this microphone instead of the default one (see: vtn devices)",
    )
    sp.add_argument(
        "--levels",
        action="store_true",
        help="stream one machine-readable line per 100ms on stderr instead of a meter:"
        " level<TAB>system dBFS<TAB>mic dBFS",
    )
    sp.set_defaults(fn=cmd_record)

    sub.add_parser(
        "menubar", help="open the menu bar recorder"
    ).set_defaults(fn=cmd_menubar)

    sub.add_parser(
        "devices", help="list the audio devices a recording can be pointed at"
    ).set_defaults(fn=cmd_devices)

    sub.add_parser(
        "projects", help="list every project and how many memos it holds"
    ).set_defaults(fn=cmd_projects)

    sp = sub.add_parser("list", help="list memos")
    sp.add_argument("--json", action="store_true", help="print memos as JSON")
    sp.add_argument("--project", help="only the memos filed under this project")
    sp.add_argument("--tag", help="only the memos whose notes carry this tag")
    sp.add_argument(
        "--sort",
        choices=services.SORTS,
        default="created",
        help="newest first by creation (default) or by last update",
    )
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("todos", help="list what the memos committed to")
    sp.add_argument("--project", help="only the to-dos of memos filed under this project")
    sp.add_argument("--all", action="store_true", help="include the ones already done")
    # a to-do is either yours or nobody's, never both, so asking for both is refused
    owner = sp.add_mutually_exclusive_group()
    owner.add_argument("--mine", action="store_true", help="only the to-dos carrying your name")
    owner.add_argument(
        "--unassigned",
        action="store_true",
        help="only the to-dos the extraction named nobody for",
    )
    sp.set_defaults(fn=cmd_todos)

    sp = sub.add_parser("todo", help="check a to-do off or put it back: todo done|open <id>")
    todo_sub = sp.add_subparsers(dest="todo_cmd", required=True)

    tsp = todo_sub.add_parser("done", help="check a to-do off: todo done <id>")
    tsp.add_argument("id", type=int)
    tsp.set_defaults(fn=cmd_todo_done)

    tsp = todo_sub.add_parser("open", help="put a to-do back on the list: todo open <id>")
    tsp.add_argument("id", type=int)
    tsp.set_defaults(fn=cmd_todo_open)

    sp = sub.add_parser("move", help="file a memo under a project: move <id> <project>")
    sp.add_argument("id", type=int)
    sp.add_argument("project")
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("delete", help="throw a memo away for good: delete <id>")
    sp.add_argument("id", type=int)
    sp.add_argument("--yes", action="store_true", help="delete without asking first")
    sp.set_defaults(fn=cmd_delete)

    sp = sub.add_parser("info", help="show what state a memo is in")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_info)

    # nested rather than flat, like `todo`: a top-level `rename` already names a
    # speaker, and hyphenating would put two more verbs in a list that is long
    # enough to read through already
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

    sp = sub.add_parser("title", help="rename a memo: title <id> <new name...>")
    sp.add_argument("id", type=int)
    sp.add_argument("name", nargs="+")
    sp.set_defaults(fn=cmd_title)

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
