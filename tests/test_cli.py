import inspect
import sys
from pathlib import Path

import pytest
from conftest import StubRepo

from voice_to_note import cli, config, services
from voice_to_note.domain import Memo, Segment, Speaker
from voice_to_note.storage.repository import Repository


def add_memo(
    repo,
    *,
    filename="standup.m4a",
    duration_s=12.0,
    language="en",
    segments=(),
    speakers=(),
    recorded_at=None,
) -> int:
    return repo.create_memo(
        filename=filename,
        wav_path=f"/tmp/{filename}.wav",
        duration_s=duration_s,
        language=language,
        segments=list(segments),
        speakers=list(speakers),
        recorded_at=recorded_at,
    )


def run(monkeypatch, repo, *argv) -> None:
    """Runs the real command line against a test database."""
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", *argv])
    cli.main()


def fresh_import(monkeypatch) -> None:
    """Says that nothing in the database was recorded when this file was, so a
    test about what the pipeline does is not also a test about the duplicate
    every `process` run asks after before it starts."""
    monkeypatch.setattr(services, "find_duplicate", lambda _repo, _src: None)


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


def test_listing_with_archived_shows_only_what_was_put_away(repo, monkeypatch, capsys):
    add_memo(repo, filename="live.m4a")
    put_away = add_memo(repo, filename="old.m4a")
    services.archive_memo(repo, put_away)

    run(monkeypatch, repo, "list", "--archived")

    out = capsys.readouterr().out
    assert "old.m4a" in out
    assert "live.m4a" not in out


def test_an_empty_archive_is_not_reported_as_an_empty_memo_list(repo, monkeypatch, capsys):
    add_memo(repo)

    run(monkeypatch, repo, "list", "--archived")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != "no memos yet\n"
    assert "archive" in captured.err.lower()


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


def test_showing_an_archived_memo_still_prints_its_transcript(repo, monkeypatch, capsys):
    # archiving hides a memo from listings only; opening it by id still works
    memo_id = add_memo(
        repo,
        segments=[Segment(0, 1500, "Hello there.", speaker="S1")],
        speakers=[Speaker("S1", "Alice")],
    )
    services.archive_memo(repo, memo_id)

    run(monkeypatch, repo, "show", str(memo_id))

    assert capsys.readouterr().out == "00:00  Alice: Hello there.\n"


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
        services,
        "memos_text",
        lambda _repo, project=None, tag=None, sort="created", archived=False: "  12  a listing line",
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


def test_the_notes_for_an_archived_memo_still_print(repo, monkeypatch, capsys):
    # archiving hides a memo from listings only; its notes still read on
    memo_id = add_memo(repo, segments=[Segment(0, 1000, "Ship it", speaker="S1")])
    repo.save_extraction(
        memo_id,
        "claude",
        {
            "title": "Sprint sync",
            "summary": "We discussed the release.",
            "action_items": [],
            "decisions": [],
            "key_insights": [],
            "open_questions": [],
            "dates": [],
            "tags": [],
        },
    )
    services.archive_memo(repo, memo_id)

    run(monkeypatch, repo, "notes", str(memo_id))

    assert "Sprint sync" in capsys.readouterr().out


# --- projects ------------------------------------------------------------


def test_listing_projects_prints_each_with_how_many_memos_it_holds(repo, monkeypatch, capsys):
    # one line per project, tab-separated: the same answer serves a person
    # reading it and anything that has to offer the projects as a choice
    add_memo(repo, filename="standup.m4a")
    moved = add_memo(repo, filename="review.m4a")
    services.move_memo(repo, moved, "work")

    run(monkeypatch, repo, "projects")

    assert capsys.readouterr().out == "other\t1\nwork\t1\n"


def test_processing_files_the_new_memo_under_a_project(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    seen: dict = {}

    def process_memo(_repo, _src, project="other", log=None, num_speakers=None, diarize=True):
        seen["project"] = project
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "run_extraction", lambda *a, **k: "claude")
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")
    fresh_import(monkeypatch)

    run(monkeypatch, StubRepo(), "process", str(src), "--project", "work")

    assert seen["project"] == "work"


def test_processing_without_a_project_files_it_under_other(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    seen: dict = {}

    def process_memo(_repo, _src, project="other", log=None, num_speakers=None, diarize=True):
        seen["project"] = project
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "run_extraction", lambda *a, **k: "claude")
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")
    fresh_import(monkeypatch)

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


def test_processing_with_an_unknown_step_never_reaches_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")

    def process_memo(*a, **k):
        raise AssertionError("process_memo must not run for an unknown step")

    monkeypatch.setattr(services, "process_memo", process_memo)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "process", str(src), "--steps", "speakers,bogus")

    assert "unknown step" in str(err.value.code)


def test_an_empty_step_list_stores_only_the_transcript(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")

    def process_memo(_repo, _src, project="other", log=None, num_speakers=None, diarize=True):
        return services.ProcessResult(1, 0, [], "en")

    def run_extraction(*a, **k):
        raise AssertionError("run_extraction must not run when notes is not among the steps")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "run_extraction", run_extraction)
    fresh_import(monkeypatch)

    run(monkeypatch, StubRepo(), "process", str(src), "--steps", "")

    assert "vtn extract 1" in capsys.readouterr().err


def test_refine_runs_before_extraction_when_both_are_asked_for(tmp_path, monkeypatch, capsys):
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    order: list[str] = []

    def process_memo(_repo, _src, project="other", log=None, num_speakers=None, diarize=True):
        order.append("process")
        return services.ProcessResult(1, 0, [], "en")

    def refine_transcript(_repo, _memo_id, dry_run=False):
        order.append("refine")
        return services.RefineResult(changes=[], flagged=[], untouched=0)

    def run_extraction(_repo, _memo_id, force=False, template="notes"):
        order.append("extract")
        return "claude"

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr(services, "refine_transcript", refine_transcript)
    monkeypatch.setattr(services, "run_extraction", run_extraction)
    monkeypatch.setattr(services, "notes", lambda *a, **k: "the notes")
    fresh_import(monkeypatch)

    run(monkeypatch, StubRepo(), "process", str(src), "--steps", "speakers,refine,notes")

    assert order == ["process", "refine", "extract"]


# --- a recording offered a second time -------------------------------------


def test_a_recording_already_stored_is_left_alone_when_the_answer_is_no(
    repo, tmp_path, monkeypatch, capsys
):
    # the same memo arrives under another name; nothing is created, and the run
    # ends the way a run that had nothing to do ends
    src = tmp_path / "standup (1).m4a"
    src.write_bytes(b"fake audio")
    stored = add_memo(repo, filename="standup.m4a", recorded_at="2026-08-17T06:01:22Z")
    monkeypatch.setattr(services.audio, "recorded_at", lambda _path: "2026-08-17T06:01:22Z")
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: pytest.fail("processed after a no")
    )
    monkeypatch.setattr("builtins.input", lambda: "n")

    run(monkeypatch, repo, "process", str(src))

    err = capsys.readouterr().err
    assert f"memo {stored} — standup.m4a" in err
    assert err.endswith("skipped\n")
    with Repository(repo.path) as after:
        assert [m.id for m in after.memos()] == [stored]


def test_a_recording_already_stored_goes_through_anyway_when_the_answer_is_yes(
    repo, tmp_path, monkeypatch, capsys
):
    # taking one recording through a second time is something people do on
    # purpose, so the question is a question rather than a refusal
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    add_memo(repo, filename="standup.m4a", recorded_at="2026-08-17T06:01:22Z")
    monkeypatch.setattr(services.audio, "recorded_at", lambda _path: "2026-08-17T06:01:22Z")
    processed: list = []

    def process_memo(_repo, path, project="other", log=None, num_speakers=None, diarize=True):
        processed.append(path)
        return services.ProcessResult(9, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process_memo)
    monkeypatch.setattr("builtins.input", lambda: "y")

    run(monkeypatch, repo, "process", str(src), "--steps", "")

    assert processed == [src.resolve()]


def test_a_recording_nothing_here_shares_a_moment_with_is_never_asked_about(
    repo, tmp_path, monkeypatch, capsys
):
    src = tmp_path / "review.m4a"
    src.write_bytes(b"fake audio")
    add_memo(repo, filename="standup.m4a", recorded_at="2026-08-17T06:01:22Z")
    monkeypatch.setattr(services.audio, "recorded_at", lambda _path: "2026-08-20T11:30:00Z")
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: services.ProcessResult(9, 0, [], "en")
    )
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("asked about a new recording"))

    run(monkeypatch, repo, "process", str(src), "--steps", "")

    assert "memo 9" in capsys.readouterr().err


def test_listing_can_be_narrowed_to_one_project(monkeypatch, capsys):
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None, sort="created", archived=False):
        seen["project"] = project
        return "   1  2026-01-01  1s  en  transcribed  work     a.m4a"

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--project", "work")

    assert seen["project"] == "work"
    assert "a.m4a" in capsys.readouterr().out


def test_listing_can_be_narrowed_to_one_tag(monkeypatch, capsys):
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None, sort="created", archived=False):
        seen["project"], seen["tag"] = project, tag
        return "   1  2026-01-01  1s  en  transcribed  work     a.m4a"

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--tag", "release")

    assert seen == {"project": None, "tag": "release"}
    assert "a.m4a" in capsys.readouterr().out


def test_a_tag_and_a_project_narrow_the_listing_together(monkeypatch, capsys):
    # both asked for means both applied, not whichever the code checked first
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None, sort="created", archived=False):
        seen["project"], seen["tag"] = project, tag
        return ""

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list", "--project", "work", "--tag", "release")

    assert seen == {"project": "work", "tag": "release"}


def test_listing_defaults_to_newest_created_and_can_be_sorted_by_update(monkeypatch, capsys):
    seen: dict = {}

    def memos_text(_repo, project=None, tag=None, sort="created", archived=False):
        seen["sort"] = sort
        return "   1  2026-01-01  1s  en  transcribed  work     a.m4a"

    monkeypatch.setattr(services, "memos_text", memos_text)

    run(monkeypatch, StubRepo(), "list")
    assert seen["sort"] == "created"

    run(monkeypatch, StubRepo(), "list", "--sort", "updated")
    assert seen["sort"] == "updated"
    assert "a.m4a" in capsys.readouterr().out


def test_a_tag_of_nothing_ends_the_listing_with_a_message(monkeypatch, capsys):
    def memos_text(_repo, project=None, tag=None, sort="created", archived=False):
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


def stored_memo(filename: str = "standup.m4a") -> Memo:
    """The one memo a delete would be asked about."""
    return Memo(3, filename, "/tmp/standup.wav", 12.0, "en", "transcribed", "2026-01-01 09:00:00")


def test_deleting_with_yes_says_what_went_without_asking(monkeypatch, capsys):
    seen: dict = {}

    def delete_memo(_repo, memo_id):
        seen["memo_id"] = memo_id
        return "standup.m4a"

    monkeypatch.setattr(services, "delete_memo", delete_memo)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("asked despite --yes"))

    run(monkeypatch, StubRepo(), "delete", "3", "--yes")

    assert seen == {"memo_id": 3}
    assert capsys.readouterr().err == "deleted memo 3 — standup.m4a\n"


def test_a_delete_answered_no_keeps_the_memo(monkeypatch, capsys):
    monkeypatch.setattr(services, "require_memo", lambda _repo, _id: stored_memo())
    monkeypatch.setattr(
        services, "delete_memo", lambda _repo, _id: pytest.fail("deleted after a no")
    )
    monkeypatch.setattr("builtins.input", lambda: "n")

    run(monkeypatch, StubRepo(), "delete", "3")

    assert capsys.readouterr().err == "delete memo 3 — standup.m4a? [y/N] kept\n"


def test_archiving_a_memo_says_what_moved_without_asking_first(monkeypatch, capsys):
    seen: dict = {}

    def archive_memo(_repo, memo_id):
        seen["memo_id"] = memo_id
        return "standup.m4a"

    monkeypatch.setattr(services, "archive_memo", archive_memo)
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("archiving deletes nothing and must not ask")
    )

    run(monkeypatch, StubRepo(), "archive", "3")

    assert seen == {"memo_id": 3}
    assert capsys.readouterr().err == "memo 3 archived — standup.m4a\n"


def test_unarchiving_a_memo_says_what_came_back_without_asking_first(monkeypatch, capsys):
    seen: dict = {}

    def unarchive_memo(_repo, memo_id):
        seen["memo_id"] = memo_id
        return "standup.m4a"

    monkeypatch.setattr(services, "unarchive_memo", unarchive_memo)
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("unarchiving deletes nothing and must not ask")
    )

    run(monkeypatch, StubRepo(), "unarchive", "3")

    assert seen == {"memo_id": 3}
    assert capsys.readouterr().err == "memo 3 unarchived — standup.m4a\n"


def test_titling_a_memo_says_what_it_is_now_called(monkeypatch, capsys):
    seen: dict = {}

    def rename_memo(_repo, memo_id, name):
        seen["memo_id"], seen["name"] = memo_id, name

    monkeypatch.setattr(services, "rename_memo", rename_memo)

    run(monkeypatch, StubRepo(), "title", "3", "Sprint", "planning")

    captured = capsys.readouterr()
    assert seen == {"memo_id": 3, "name": "Sprint planning"}
    assert captured.out == ""
    assert captured.err == "memo 3: renamed to Sprint planning\n"


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


def test_template_new_routes_the_name_and_source_to_services(monkeypatch, capsys):
    seen = {}

    def fake_template_new(name, source="notes"):
        seen["call"] = (name, source)
        return "created it"

    monkeypatch.setattr(services, "template_new", fake_template_new)

    run(monkeypatch, StubRepo(), "template", "new", "standup", "--from", "lecture")

    assert seen["call"] == ("standup", "lecture")
    assert capsys.readouterr().err == "created it\n"


def test_template_new_starts_from_notes_when_no_source_is_given(monkeypatch, capsys):
    seen = {}

    def fake_template_new(name, source="notes"):
        seen["call"] = (name, source)
        return "created it"

    monkeypatch.setattr(services, "template_new", fake_template_new)

    run(monkeypatch, StubRepo(), "template", "new", "standup")

    assert seen["call"] == ("standup", "notes")


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


# --- opening the menu bar recorder ----------------------------------------


def built_menubar_app(monkeypatch, tmp_path) -> Path:
    """The app bundle setup would have built, where the command looks for it."""
    app = tmp_path / "VTN Recorder.app"
    binary = app / "Contents" / "MacOS" / "vtn-menubar"
    binary.parent.mkdir(parents=True)
    binary.write_text("bin")
    monkeypatch.setattr(cli.config, "MENUBAR_APP", app)
    monkeypatch.setattr(cli.config, "MENUBAR_BIN", binary)
    return app


def test_the_menu_bar_recorder_is_handed_to_the_finder_to_open(monkeypatch, tmp_path):
    # opened as an app it outlives this command, and macOS attributes the
    # recording permissions to the bundle rather than to whatever launched it
    monkeypatch.setattr(sys, "platform", "darwin")
    app = built_menubar_app(monkeypatch, tmp_path)
    seen: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **_kwargs: seen.update(cmd=cmd))

    run(monkeypatch, StubRepo(), "menubar")

    assert seen["cmd"] == ["open", str(app)]


def test_previewing_the_menu_bar_recorder_runs_the_binary_itself_with_the_flag(
    monkeypatch, tmp_path
):
    # `open` raises an app that is already running and drops the arguments it
    # was handed, so asking for a preview that way would get the real recorder
    monkeypatch.setattr(sys, "platform", "darwin")
    built_menubar_app(monkeypatch, tmp_path)
    seen: dict = {}
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **_kwargs: seen.update(cmd=cmd))

    run(monkeypatch, StubRepo(), "menubar", "--preview")

    assert seen["cmd"] == [str(cli.config.MENUBAR_BIN), "--preview"]


def test_opening_the_menu_bar_recorder_before_setup_says_how_to_build_it(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.config, "MENUBAR_BIN", tmp_path / "never-built")

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "menubar")

    assert "vtn setup" in err.value.code


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


# --- the to-do list ------------------------------------------------------


def test_the_to_do_list_prints_what_is_still_outstanding(repo, monkeypatch, capsys):
    memo_id = add_memo(repo)
    repo.sync_todos(
        memo_id, [{"task": "Cut the release", "owner": "Alice", "deadline": "Friday"}]
    )
    (todo,) = repo.todos()

    run(monkeypatch, repo, "todos")

    assert capsys.readouterr().out == (
        f"{todo.id:>4}  [ ]  Cut the release  Alice  Friday  memo {memo_id} (other)\n"
    )


def test_the_mine_flag_narrows_the_to_do_list_to_tasks_carrying_your_name(
    repo, monkeypatch, capsys
):
    monkeypatch.setattr(config, "MY_NAME", "Alex")
    memo_id = add_memo(repo)
    repo.sync_todos(
        memo_id,
        [
            {"task": "Cut the release", "owner": "Alex", "deadline": "Friday"},
            {"task": "Write the changelog", "owner": "Alice", "deadline": None},
        ],
    )
    mine = next(t for t in repo.todos() if t.owner == "Alex")

    run(monkeypatch, repo, "todos", "--mine")

    assert capsys.readouterr().out == (
        f"{mine.id:>4}  [ ]  Cut the release  Alex  Friday  memo {memo_id} (other)\n"
    )


def test_the_unassigned_flag_narrows_the_to_do_list_to_tasks_nobody_was_named_for(
    repo, monkeypatch, capsys
):
    memo_id = add_memo(repo)
    repo.sync_todos(
        memo_id,
        [
            {"task": "Cut the release", "owner": "Alice", "deadline": "Friday"},
            {"task": "Water the plants", "owner": None, "deadline": None},
        ],
    )
    unassigned = next(t for t in repo.todos() if not t.owner)

    run(monkeypatch, repo, "todos", "--unassigned")

    assert capsys.readouterr().out == (
        f"{unassigned.id:>4}  [ ]  Water the plants      memo {memo_id} (other)\n"
    )


def test_asking_for_mine_and_unassigned_together_is_refused_by_the_parser(repo, monkeypatch):
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, repo, "todos", "--mine", "--unassigned")

    assert err.value.code == 2


def test_nothing_left_to_do_says_so_without_polluting_stdout(repo, monkeypatch, capsys):
    run(monkeypatch, repo, "todos")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no to-dos\n"


def test_checking_a_to_do_off_says_so_and_takes_it_off_the_list(repo, monkeypatch, capsys):
    memo_id = add_memo(repo)
    repo.sync_todos(memo_id, [{"task": "Cut the release", "owner": None, "deadline": None}])
    (todo,) = repo.todos()

    run(monkeypatch, repo, "todo", "done", str(todo.id))

    assert capsys.readouterr().err == f"to-do {todo.id} done\n"
    # the command closed the database it was handed, as every command does
    with Repository(repo.path) as reopened:
        assert reopened.todos() == []
        assert [t.status for t in reopened.todos(include_done=True)] == ["done"]


# --- chat ----------------------------------------------------------------


def test_chat_opens_a_conversation_over_the_memos_and_prints_the_answer(monkeypatch, capsys):
    seen: dict = {}
    monkeypatch.setattr(
        services, "start_chat", lambda _r, ids, title="": seen.update(ids=ids, title=title) or 7
    )
    monkeypatch.setattr(
        services,
        "chat",
        lambda _r, cid, asked: seen.update(cid=cid, asked=asked)
        or services.ChatTurn("claude", "Friday."),
    )

    run(monkeypatch, StubRepo(), "chat", "3,5", "when", "do", "they", "ship?", "--title", "ship")

    out, err = capsys.readouterr()
    assert seen == {"ids": [3, 5], "title": "ship", "cid": 7, "asked": "when do they ship?"}
    assert out == "Friday.\n"
    assert err == "(claude) conversation 7\n\n"


def test_chat_continues_a_conversation_by_its_id(monkeypatch, capsys):
    seen: dict = {}
    monkeypatch.setattr(
        services, "start_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened"))
    )
    monkeypatch.setattr(
        services,
        "chat",
        lambda _r, cid, asked: seen.update(cid=cid, asked=asked)
        or services.ChatTurn("ollama/q", "yes"),
    )

    run(monkeypatch, StubRepo(), "chat", "-c", "7", "sure?")

    assert seen == {"cid": 7, "asked": "sure?"}
    assert capsys.readouterr().out == "yes\n"


def test_chat_without_a_question_prints_the_thread_as_text_or_json(monkeypatch, capsys):
    monkeypatch.setattr(services, "chat_text", lambda _r, cid: f"thread {cid}")
    monkeypatch.setattr(services, "chat_json", lambda _r, cid: f'{{"id": {cid}}}')

    run(monkeypatch, StubRepo(), "chat", "-c", "7")
    assert capsys.readouterr().out == "thread 7\n"

    run(monkeypatch, StubRepo(), "chat", "-c", "7", "--json")
    assert capsys.readouterr().out == '{"id": 7}\n'


def test_chat_with_nothing_to_go_on_says_what_it_needs(monkeypatch, capsys):
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "chat")
    assert "memo ids" in str(err.value.code)


def test_chat_refuses_memo_ids_it_cannot_read(monkeypatch, capsys):
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "chat", "three", "hi")
    assert "memo ids are numbers" in str(err.value.code)


def test_chat_can_rename_or_delete_a_conversation(monkeypatch, capsys):
    seen: dict = {}
    monkeypatch.setattr(services, "rename_chat", lambda _r, cid, t: seen.update(renamed=(cid, t)))
    monkeypatch.setattr(services, "delete_chat", lambda _r, cid: seen.update(deleted=cid))

    run(monkeypatch, StubRepo(), "chat", "-c", "7", "--rename", "pricing")
    run(monkeypatch, StubRepo(), "chat", "-c", "8", "--delete")

    assert seen == {"renamed": (7, "pricing"), "deleted": 8}
    assert "conversation 7 renamed" in capsys.readouterr().err


def test_chatting_about_a_memo_that_is_not_there_ends_the_command_with_a_message(
    monkeypatch, capsys
):
    def start_chat(_r, ids, title=""):
        raise services.NotFound("no memo with id 999")

    monkeypatch.setattr(services, "start_chat", start_chat)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, StubRepo(), "chat", "999", "hi")

    assert err.value.code == "no memo with id 999"


def test_chats_lists_conversations_as_text_or_json_narrowed_to_a_memo(monkeypatch, capsys):
    monkeypatch.setattr(services, "chats_text", lambda _r, memo_id=None: f"text {memo_id}")
    monkeypatch.setattr(services, "chats_json", lambda _r, memo_id=None: f"json {memo_id}")

    run(monkeypatch, StubRepo(), "chats")
    assert capsys.readouterr().out == "text None\n"

    run(monkeypatch, StubRepo(), "chats", "--json", "--memo", "3")
    assert capsys.readouterr().out == "json 3\n"
