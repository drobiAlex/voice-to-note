import inspect
import sys
from pathlib import Path

import pytest
from conftest import StubRepo

from voice_to_note import cli, services
from voice_to_note.domain import Segment, Speaker


def add_memo(
    repo, *, filename="standup.m4a", duration_s=12.0, language="en", segments=(), speakers=()
) -> int:
    return repo.create_memo(
        filename=filename,
        wav_path=f"/tmp/{filename}.wav",
        duration_s=duration_s,
        language=language,
        segments=list(segments),
        speakers=list(speakers),
    )


def run(monkeypatch, repo, *argv) -> None:
    """Runs the real command line against a test database."""
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", *argv])
    cli.main()


def listing_line(memo, dur: str) -> str:
    """The one memo-per-line shape `vtn list` has always printed."""
    return (
        f"{memo.id:>4}  {memo.created_at}  {dur:>10}  {memo.language or '?':<3}"
        f"  {memo.status:<12} {memo.project:<8} {memo.filename}"
    )


# --- the text services owe the command line -----------------------------


def test_the_memo_listing_reads_the_way_it_always_has(repo):
    add_memo(repo)
    # a memo still being processed has neither a duration nor a language yet
    add_memo(repo, filename="notes.m4a", duration_s=0.0, language="")
    newest, oldest = repo.memos()

    assert services.memos_text(repo) == "\n".join(
        [listing_line(newest, "?"), listing_line(oldest, "12s")]
    )


def test_an_empty_memo_listing_produces_nothing_to_print(repo):
    assert services.memos_text(repo) == ""


def test_a_transcript_reads_the_way_it_always_has(repo):
    memo_id = add_memo(
        repo,
        segments=[
            Segment(0, 1500, "Hello there.", speaker="S1"),
            Segment(62000, 63000, "Second one.", speaker=None),
        ],
        speakers=[Speaker("S1", "Alice")],
    )

    assert services.transcript_lines(repo, memo_id) == (
        "00:00  Alice: Hello there.\n01:02  Unknown: Second one."
    )


def test_a_memo_with_no_transcript_produces_nothing_to_print(repo):
    assert services.transcript_lines(repo, add_memo(repo)) == ""


def test_a_transcript_heading_names_the_memo_and_where_it_got_to(repo):
    memo_id = add_memo(repo)

    assert services.memo_heading(repo, memo_id) == f"memo {memo_id} — standup.m4a (transcribed)"


def test_a_heading_for_a_memo_that_was_never_stored_is_refused(repo):
    with pytest.raises(services.NotFound):
        services.memo_heading(repo, 999)


# --- what the user actually sees ----------------------------------------


def test_listing_memos_prints_one_line_each_newest_first(repo, monkeypatch, capsys):
    add_memo(repo)
    add_memo(repo, filename="notes.m4a", duration_s=0.0, language="")
    newest, oldest = repo.memos()

    run(monkeypatch, repo, "list")

    out = capsys.readouterr().out
    assert out == listing_line(newest, "?") + "\n" + listing_line(oldest, "12s") + "\n"


def test_listing_nothing_says_so_without_polluting_stdout(repo, monkeypatch, capsys):
    run(monkeypatch, repo, "list")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no memos yet\n"


def test_showing_a_memo_keeps_the_heading_off_stdout(repo, monkeypatch, capsys):
    # the heading is progress information; stdout stays pipeable
    memo_id = add_memo(
        repo,
        segments=[Segment(0, 1500, "Hello there.", speaker="S1")],
        speakers=[Speaker("S1", "Alice")],
    )

    run(monkeypatch, repo, "show", str(memo_id))

    captured = capsys.readouterr()
    assert captured.out == "00:00  Alice: Hello there.\n"
    assert captured.err == f"memo {memo_id} — standup.m4a (transcribed)\n\n"


def test_showing_a_memo_that_was_never_stored_prints_no_json(repo, monkeypatch, capsys):
    # the check has to come before the --json branch, or scripts get an empty
    # transcript back for an id that does not exist
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, repo, "show", "999", "--json")

    assert err.value.code == "no memo with id 999"
    assert capsys.readouterr().out == ""


# --- the command line as a thin layer -----------------------------------


def test_listing_prints_exactly_what_services_formatted(monkeypatch, capsys):
    monkeypatch.setattr(
        services, "memos_text", lambda _repo, project=None, tag=None: "  12  a listing line"
    )

    run(monkeypatch, StubRepo(), "list")

    assert capsys.readouterr().out == "  12  a listing line\n"


def test_showing_prints_exactly_what_services_formatted(monkeypatch, capsys):
    monkeypatch.setattr(services, "memo_heading", lambda _repo, _id: "memo 1 — a.m4a (new)")
    monkeypatch.setattr(
        services, "transcript_lines", lambda _repo, _id, raw=False: "00:00  Alice: Hi"
    )

    run(monkeypatch, StubRepo(), "show", "1")

    captured = capsys.readouterr()
    assert captured.out == "00:00  Alice: Hi\n"
    assert captured.err == "memo 1 — a.m4a (new)\n\n"


def test_the_command_line_leaves_database_reads_and_formatting_to_services():
    # cli composes the app: it may build a Repository, but a query or a format
    # string here is a second place for the same logic to drift.
    # Grepping the source is crude — it leans on the binding being named `repo`,
    # where an AST walk would not. Kept deliberately: this catches the drift that
    # actually happens (a query creeping back in) for a fraction of the machinery.
    source = Path(inspect.getsourcefile(cli)).read_text()

    assert "Repository(" in source
    assert "repo." not in source
    assert "from .transforms" not in source


# --- repairing a transcript ----------------------------------------------


def refine_result():
    """One repaired line, one refused, the rest left as they were."""
    return services.RefineResult(
        changes=[services.Change(3, "so their going", "So they're going")],
        flagged=[7],
        untouched=16,
    )


def test_refining_a_memo_reports_what_it_changed(monkeypatch, capsys):
    seen: dict = {}

    def refine_transcript(_repo, memo_id, dry_run=False):
        seen["memo_id"], seen["dry_run"] = memo_id, dry_run
        return refine_result()

    monkeypatch.setattr(services, "refine_transcript", refine_transcript)

    run(monkeypatch, StubRepo(), "refine", "3")

    captured = capsys.readouterr()
    assert seen == {"memo_id": 3, "dry_run": False}
    assert captured.out == ""
    assert captured.err == "memo 3: repaired 1, flagged 1, unchanged 16\n"


def test_a_diff_run_prints_the_changes_and_writes_nothing(monkeypatch, capsys):
    seen: dict = {}

    def refine_transcript(_repo, memo_id, dry_run=False):
        seen["dry_run"] = dry_run
        return refine_result()

    monkeypatch.setattr(services, "refine_transcript", refine_transcript)
    monkeypatch.setattr(services, "refine_diff_text", lambda _r: "[3] before\n      → after")

    run(monkeypatch, StubRepo(), "refine", "3", "--diff")

    captured = capsys.readouterr()
    assert seen["dry_run"] is True
    assert captured.out == "[3] before\n      → after\n"
    assert captured.err == ""


def test_a_diff_run_that_found_nothing_prints_nothing_at_all(monkeypatch, capsys):
    # a lone blank line would read as output; it has to be silence
    nothing = services.RefineResult(changes=[], flagged=[], untouched=17)
    monkeypatch.setattr(services, "refine_transcript", lambda *a, **k: nothing)

    run(monkeypatch, StubRepo(), "refine", "3", "--diff")

    assert capsys.readouterr().out == ""


def test_showing_the_transcription_as_heard_asks_services_for_it(monkeypatch, capsys):
    seen: dict = {}

    def lines(_repo, _id, raw=False):
        seen["raw"] = raw
        return "00:00  Alice: as heard"

    monkeypatch.setattr(services, "memo_heading", lambda _repo, _id: "memo 1 — a.m4a (new)")
    monkeypatch.setattr(services, "transcript_lines", lines)

    run(monkeypatch, StubRepo(), "show", "1", "--raw")

    assert seen["raw"] is True
    assert capsys.readouterr().out == "00:00  Alice: as heard\n"


def test_the_raw_flag_reaches_the_json_output_too(monkeypatch, capsys):
    # a script asking for the original should not have to parse the human form
    seen: dict = {}

    def as_json(_repo, _id, raw=False):
        seen["raw"] = raw
        return '[{"text": "as heard"}]'

    monkeypatch.setattr(services, "require_memo", lambda _repo, _id: None)
    monkeypatch.setattr(services, "transcript_json", as_json)

    run(monkeypatch, StubRepo(), "show", "1", "--raw", "--json")

    assert seen["raw"] is True
    assert capsys.readouterr().out == '[{"text": "as heard"}]\n'


def test_the_info_command_prints_what_state_a_memo_is_in(monkeypatch, capsys):
    seen: dict = {}

    def memo_info_text(_repo, memo_id):
        seen["id"] = memo_id
        return "file:     standup.m4a"

    monkeypatch.setattr(services, "memo_info_text", memo_info_text)

    run(monkeypatch, StubRepo(), "info", "3")

    assert seen == {"id": 3}
    assert capsys.readouterr().out == "file:     standup.m4a\n"


def test_asking_after_a_memo_that_is_not_there_ends_the_command_with_a_message(
    monkeypatch, capsys
):
    def memo_info_text(_repo, _id):
        raise services.NotFound("no memo with id 999")

    monkeypatch.setattr(services, "memo_info_text", memo_info_text)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "info", "999")

    assert err.value.code == "no memo with id 999"


# --- notes at the command line -------------------------------------------


def test_the_notes_command_can_print_the_note_as_the_screen_shows_it(monkeypatch, capsys):
    # the TUI note panel shows an edit where somebody made one and the generated
    # notes otherwise; --edited is that same text without opening the screen
    monkeypatch.setattr(services, "notes_markdown", lambda _repo, _id: "# In my own words")

    run(monkeypatch, StubRepo(), "notes", "3", "--edited")

    assert capsys.readouterr().out == "# In my own words\n"


def test_asking_for_the_edited_note_as_json_is_refused(monkeypatch, capsys):
    # --json prints the stored extraction, which is the thing an edit replaces;
    # answering both at once would mean printing one and calling it the other
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "notes", "3", "--edited", "--json")

    assert err.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_the_notes_command_still_prints_the_extraction_by_default(monkeypatch, capsys):
    monkeypatch.setattr(services, "notes", lambda _repo, _id: "the notes")

    run(monkeypatch, StubRepo(), "notes", "3")

    assert capsys.readouterr().out == "the notes\n"


# --- projects ------------------------------------------------------------


def test_processing_files_the_new_memo_under_a_project(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    seen: dict = {}

    def process_memo(_repo, _src, project="other", log=None):
        seen["project"] = project
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "run_extraction", lambda *a, **k: "claude")
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")

    run(monkeypatch, StubRepo(), "process", str(src), "--project", "work")

    assert seen["project"] == "work"


def test_processing_without_a_project_files_it_under_other(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    seen: dict = {}

    def process_memo(_repo, _src, project="other", log=None):
        seen["project"] = project
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "run_extraction", lambda *a, **k: "claude")
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")

    run(monkeypatch, StubRepo(), "process", str(src))

    assert seen["project"] == "other"


def test_processing_with_an_unknown_template_never_reaches_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")

    def process_memo(*a, **k):
        raise AssertionError("process_memo must not run for an unknown template")

    monkeypatch.setattr(services, "process_memo", process_memo)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "process", str(src), "--template", "bogus")

    assert "unknown note template" in str(err.value.code)


def test_listing_can_be_narrowed_to_one_project(monkeypatch, capsys):
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None):
        seen["project"] = project
        return "   1  2026-01-01  1s  en  transcribed  work     a.m4a"

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--project", "work")

    assert seen["project"] == "work"
    assert "a.m4a" in capsys.readouterr().out


def test_listing_can_be_narrowed_to_one_tag(monkeypatch, capsys):
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None):
        seen["project"], seen["tag"] = project, tag
        return "   1  2026-01-01  1s  en  transcribed  work     a.m4a"

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--tag", "release")

    assert seen == {"project": None, "tag": "release"}
    assert "a.m4a" in capsys.readouterr().out


def test_a_tag_and_a_project_narrow_the_listing_together(monkeypatch, capsys):
    # both asked for means both applied, not whichever the code checked first
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None):
        seen["project"], seen["tag"] = project, tag
        return ""

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--project", "work", "--tag", "release")

    assert seen == {"project": "work", "tag": "release"}


def test_a_tag_of_nothing_ends_the_listing_with_a_message(monkeypatch, capsys):
    def memos_text(_repo, project=None, tag=None):
        raise services.InvalidInput("a tag needs some text")

    monkeypatch.setattr(services, "memos_text", memos_text)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "list", "--tag", "  ")

    assert err.value.code == "a tag needs some text"


def test_moving_a_memo_says_where_it_went(monkeypatch, capsys):
    seen: dict = {}

    def move_memo(_repo, memo_id, project):
        seen["memo_id"], seen["project"] = memo_id, project

    monkeypatch.setattr(services, "move_memo", move_memo)

    run(monkeypatch, StubRepo(), "move", "3", "side")

    captured = capsys.readouterr()
    assert seen == {"memo_id": 3, "project": "side"}
    assert captured.out == ""
    assert captured.err == "memo 3 moved to side\n"


def test_renaming_a_project_says_how_many_memos_it_carried(monkeypatch, capsys):
    seen: dict = {}

    def rename_project(_repo, old, new):
        seen["old"], seen["new"] = old, new
        return 3

    monkeypatch.setattr(services, "rename_project", rename_project)

    run(monkeypatch, StubRepo(), "project", "rename", "work", "client")

    assert seen == {"old": "work", "new": "client"}
    assert capsys.readouterr().err == "3 memos moved to client\n"


def test_emptying_a_project_says_where_its_one_memo_went(monkeypatch, capsys):
    # one memo is the common case for a project somebody is tidying away
    seen: dict = {}

    def remove_project(_repo, name):
        seen["name"] = name
        return 1

    monkeypatch.setattr(services, "remove_project", remove_project)

    run(monkeypatch, StubRepo(), "project", "remove", "work")

    assert seen == {"name": "work"}
    assert capsys.readouterr().err == "1 memo moved to other\n"


def test_renaming_a_project_to_nothing_ends_the_command_with_a_message(monkeypatch, capsys):
    def rename_project(_repo, _old, _new):
        raise services.InvalidInput("a project needs a name")

    monkeypatch.setattr(services, "rename_project", rename_project)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "project", "rename", "work", "")

    assert err.value.code == "a project needs a name"


def test_a_nameless_project_ends_the_command_with_a_message(monkeypatch, capsys):
    def move_memo(_repo, _id, _project):
        raise services.InvalidInput("a project needs a name")

    monkeypatch.setattr(services, "move_memo", move_memo)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "move", "3", "")

    assert err.value.code == "a project needs a name"


def test_the_tui_command_opens_the_browser_on_the_memo_database(monkeypatch):
    # the app is imported inside the command, so patching the module works and
    # every other command still avoids paying for textual
    from voice_to_note.tui import app as tui_app

    opened: dict = {}

    class FakeApp:
        def __init__(self, repo):
            opened["repo"] = repo

        def run(self):
            opened["ran"] = True

    monkeypatch.setattr(tui_app, "MemoApp", FakeApp)
    stub = StubRepo()

    run(monkeypatch, stub, "tui")

    assert opened["repo"] is stub
    assert opened["ran"] is True


def test_re_extracting_can_be_told_to_overwrite_an_edited_note(monkeypatch, capsys):
    seen: dict = {}

    def run_extraction(_repo, memo_id, force=False, template="notes"):
        seen["memo_id"], seen["force"] = memo_id, force
        return "claude"

    monkeypatch.setattr(services, "require_memo", lambda _repo, _id: None)
    monkeypatch.setattr(services, "run_extraction", run_extraction)
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")

    run(monkeypatch, StubRepo(), "extract", "3", "--force")

    assert seen == {"memo_id": 3, "force": True}


def test_diarizing_can_be_told_how_many_speakers_to_look_for(monkeypatch, capsys):
    seen: dict = {}

    def rediarize(_repo, memo_id, log=None, num_speakers=None):
        seen["memo_id"], seen["num_speakers"] = memo_id, num_speakers
        return ["S1", "S2", "S3"]

    monkeypatch.setattr(services, "rediarize", rediarize)

    run(monkeypatch, StubRepo(), "diarize", "3", "--speakers", "3")

    assert seen == {"memo_id": 3, "num_speakers": 3}


def test_diarizing_without_a_speaker_count_leaves_it_to_auto_detect(monkeypatch, capsys):
    seen: dict = {}

    def rediarize(_repo, memo_id, log=None, num_speakers=None):
        seen["num_speakers"] = num_speakers
        return ["S1"]

    monkeypatch.setattr(services, "rediarize", rediarize)

    run(monkeypatch, StubRepo(), "diarize", "3")

    assert seen["num_speakers"] is None


# --- installing whisper.cpp and its models --------------------------------


def test_the_setup_command_forwards_progress_and_completion_to_stdout(monkeypatch, capsys):
    def setup(log, download):
        log("cloning whisper.cpp …")
        return "setup complete"

    monkeypatch.setattr(services, "setup", setup)

    run(monkeypatch, StubRepo(), "setup")

    assert capsys.readouterr().out == "cloning whisper.cpp …\nsetup complete\n"


def test_the_setup_command_overwrites_its_own_download_line_on_stderr(monkeypatch, capsys):
    def setup(log, download):
        download("  model.bin 1.0/2.0 MB (50%)")
        download("  model.bin 2.0/2.0 MB (100%)")
        log("done")
        return "setup complete"

    monkeypatch.setattr(services, "setup", setup)

    run(monkeypatch, StubRepo(), "setup")

    out = capsys.readouterr()
    assert out.out == "done\nsetup complete\n"
    assert out.err == "\r  model.bin 1.0/2.0 MB (50%)\r  model.bin 2.0/2.0 MB (100%)\n"


# --- settings --------------------------------------------------------------


def test_the_config_command_prints_the_rows_services_provided(monkeypatch, capsys):
    monkeypatch.setattr(
        services,
        "config_rows",
        lambda: [("num_speakers", "-1", "default", "speaker count to assume; -1 auto-detects")],
    )

    run(monkeypatch, StubRepo(), "config")

    out = capsys.readouterr().out
    assert "num_speakers" in out
    assert "-1" in out
    assert "default" in out
    assert "speaker count to assume" in out


def test_config_set_routes_the_typed_key_and_value_to_services(monkeypatch, capsys):
    seen = {}

    def fake_config_set(key, value):
        seen["call"] = (key, value)
        return "whisper_model set to small"

    monkeypatch.setattr(services, "config_set", fake_config_set)

    run(monkeypatch, StubRepo(), "config", "set", "whisper_model", "small")

    assert seen["call"] == ("whisper_model", "small")
    assert capsys.readouterr().err == "whisper_model set to small\n"


def test_template_show_routes_the_typed_name_to_services(monkeypatch, capsys):
    seen = {}

    def fake_template_text(name):
        seen["call"] = name
        return "a custom notes prompt"

    monkeypatch.setattr(services, "template_text", fake_template_text)

    run(monkeypatch, StubRepo(), "template", "show", "notes")

    assert seen["call"] == "notes"
    assert capsys.readouterr().out == "a custom notes prompt\n"


def test_template_reset_routes_the_typed_name_to_services(monkeypatch, capsys):
    seen = {}

    def fake_template_reset(name):
        seen["call"] = name
        return "notes restored to built-in"

    monkeypatch.setattr(services, "template_reset", fake_template_reset)

    run(monkeypatch, StubRepo(), "template", "reset", "notes")

    assert seen["call"] == "notes"
    assert capsys.readouterr().err == "notes restored to built-in\n"


def test_the_bare_template_command_lists_names_and_status(monkeypatch, capsys):
    monkeypatch.setattr(
        services, "template_rows", lambda: [("notes", "built-in"), ("refine", "overridden")]
    )

    run(monkeypatch, StubRepo(), "template")

    out = capsys.readouterr().out
    assert "notes" in out
    assert "built-in" in out
    assert "refine" in out
    assert "overridden" in out


# --- launching bare, with no subcommand -----------------------------------


def test_bare_invocation_opens_the_tui_once_setup_has_run(monkeypatch):
    monkeypatch.setattr(services, "ready", lambda: True)
    called = []
    monkeypatch.setattr(cli, "cmd_tui", lambda args: called.append(args))

    run(monkeypatch, StubRepo())

    assert len(called) == 1


def test_bare_invocation_without_setup_tells_the_user_to_run_it(monkeypatch):
    monkeypatch.setattr(services, "ready", lambda: False)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo())

    assert "vtn setup" in err.value.code


def test_explicit_setup_runs_regardless_of_readiness(monkeypatch, capsys):
    monkeypatch.setattr(services, "ready", lambda: False)
    monkeypatch.setattr(services, "setup", lambda log, download: "setup complete")

    run(monkeypatch, StubRepo(), "setup")

    assert capsys.readouterr().out == "setup complete\n"


def test_the_setup_command_can_preview_with_a_mock_world(monkeypatch, capsys):
    captured: dict = {}

    def setup(log, download, world=None):
        captured["world"] = world
        return "setup complete"

    monkeypatch.setattr(services, "setup", setup)

    run(monkeypatch, StubRepo(), "setup", "--mock")

    assert isinstance(captured["world"], services.World)
    assert capsys.readouterr().out == "setup complete\n"


def test_naming_a_speaker_nothing_ends_the_command_with_a_message(monkeypatch, capsys):
    def rename_speaker(_repo, _memo_id, _label, _name):
        raise services.InvalidInput("a speaker needs a name")

    monkeypatch.setattr(services, "rename_speaker", rename_speaker)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "rename", "3", "S1", "")

    assert err.value.code == "a speaker needs a name"
