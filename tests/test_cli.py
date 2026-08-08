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
        f"{memo.id:>4}  {memo.created_at}  {dur:>6}  {memo.language or '?':<3}"
        f"  {memo.status:<12} {memo.filename}"
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
    monkeypatch.setattr(services, "memos_text", lambda _repo: "  12  a listing line")

    run(monkeypatch, StubRepo(), "list")

    assert capsys.readouterr().out == "  12  a listing line\n"


def test_showing_prints_exactly_what_services_formatted(monkeypatch, capsys):
    monkeypatch.setattr(services, "memo_heading", lambda _repo, _id: "memo 1 — a.m4a (new)")
    monkeypatch.setattr(services, "transcript_lines", lambda _repo, _id: "00:00  Alice: Hi")

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
