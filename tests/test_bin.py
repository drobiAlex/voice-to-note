"""The bin: what a delete moves out of sight, what it keeps, and every place a
binned memo must not show up."""

import json
import sqlite3
import sys

import numpy as np
import pytest

from voice_to_note import cli, services
from voice_to_note.domain import Segment, Speaker
from voice_to_note.storage.repository import Repository
from voice_to_note.transforms.youtube import YouTubeVideo

NOTES = {
    "title": "Sprint sync",
    "summary": "We agreed to ship on Friday.",
    "action_items": [{"task": "Cut the release", "owner": "Alice", "deadline": "Friday"}],
    "decisions": ["Ship on Friday"],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": ["release"],
}


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "memo.wav"
    path.write_bytes(b"RIFF" + bytes(range(200)))
    return path


def full_memo(repo, wav, filename="standup.m4a", project="work") -> int:
    """A memo with everything a delete must not lose: words, a named voice with
    a fingerprint, notes, a to-do somebody checked off, and a project."""
    memo_id = repo.create_memo(
        filename=filename,
        wav_path=str(wav),
        duration_s=1.0,
        language="en",
        segments=[Segment(0, 1000, "we ship on friday", speaker="S1")],
        speakers=[Speaker("S1", "Alice", np.array([1.0, 0.0], dtype=np.float32))],
        project=project,
    )
    repo.save_extraction(memo_id, "claude", NOTES)
    # worded per memo: a task already open elsewhere in the project is a repeat
    # and is not filed twice
    repo.sync_todos(
        memo_id, [{"task": f"Cut the release of {filename}", "owner": "Alice", "deadline": "Friday"}]
    )
    return memo_id


# --- the round trip ---------------------------------------------------------


def test_a_binned_and_restored_memo_comes_back_with_everything_it_went_in_with(repo, wav):
    memo_id = full_memo(repo, wav)
    todo = repo.todos()[0]
    repo.set_todo_status(todo.id, "done")
    repo.save_notes_md(memo_id, "my own notes")
    chat = services.start_chat(repo, [memo_id])
    repo.set_archived(memo_id, True)
    before = (
        repo.memo(memo_id),
        repo.segments(memo_id),
        repo.display_names(memo_id),
        repo.extraction(memo_id),
        repo.todos(include_done=True, memo_id=memo_id),
        repo.notes_md(memo_id),
        repo.con.execute("SELECT embedding FROM speakers").fetchone()[0],
    )
    wav_bytes = wav.read_bytes()

    services.delete_memo(repo, memo_id)
    assert wav.read_bytes() == wav_bytes
    services.restore_memo(repo, memo_id)

    after = (
        repo.memo(memo_id),
        repo.segments(memo_id),
        repo.display_names(memo_id),
        repo.extraction(memo_id),
        repo.todos(include_done=True, memo_id=memo_id),
        repo.notes_md(memo_id),
        repo.con.execute("SELECT embedding FROM speakers").fetchone()[0],
    )
    assert after == before
    assert wav.read_bytes() == wav_bytes
    assert repo.memo(memo_id).archived_at is not None
    assert repo.memo(memo_id).project == "work"
    assert repo.todos(include_done=True, memo_id=memo_id)[0].status == "done"
    assert repo.conversation(chat).memo_ids == (memo_id,)


def test_deleting_a_memo_removes_nothing_from_disk_or_the_database(repo, wav):
    memo_id = full_memo(repo, wav)

    assert services.delete_memo(repo, memo_id) == "standup.m4a"

    assert wav.exists()
    assert repo.memo(memo_id).deleted_at is not None
    assert repo.segments(memo_id) != []
    assert repo.extraction(memo_id) is not None


def test_deleting_a_memo_already_in_the_bin_keeps_the_date_it_went_in(repo, wav):
    memo_id = full_memo(repo, wav)
    services.delete_memo(repo, memo_id)
    repo.con.execute("UPDATE memos SET deleted_at='2020-01-01 00:00:00' WHERE id=?", (memo_id,))

    services.delete_memo(repo, memo_id)

    assert repo.memo(memo_id).deleted_at == "2020-01-01 00:00:00"


def test_restoring_a_memo_that_is_not_in_the_bin_is_not_an_error(repo, wav):
    memo_id = full_memo(repo, wav)

    assert services.restore_memo(repo, memo_id) == "standup.m4a"
    assert repo.memo(memo_id).deleted_at is None


def test_binning_or_restoring_a_memo_that_never_existed_is_refused(repo):
    with pytest.raises(services.NotFound):
        services.delete_memo(repo, 99)
    with pytest.raises(services.NotFound):
        services.restore_memo(repo, 99)


# --- where a binned memo must not show --------------------------------------


def test_a_binned_memo_is_in_no_listing_the_live_or_the_archived_one(repo, wav):
    live = full_memo(repo, wav, "live.m4a")
    gone = full_memo(repo, wav, "gone.m4a")
    put_away = full_memo(repo, wav, "away.m4a")
    repo.set_archived(put_away, True)
    gone_archived = full_memo(repo, wav, "both.m4a")
    repo.set_archived(gone_archived, True)
    services.delete_memo(repo, gone)
    services.delete_memo(repo, gone_archived)

    assert [m.id for m in services.memos(repo)] == [live]
    assert [m.id for m in services.memos(repo, archived=True)] == [put_away]
    assert [r.id for r in services.memo_rows(repo)] == [live]
    assert [r.id for r in services.memo_rows(repo, archived=True)] == [put_away]
    assert [r.id for r in services.memo_rows(repo, binned=True)] == [gone_archived, gone]
    assert "gone.m4a" not in services.memos_text(repo)
    assert "both.m4a" not in services.memos_text(repo, archived=True)
    assert [m["filename"] for m in json.loads(services.memos_json(repo))] == ["live.m4a"]
    assert services.archived_count(repo) == 1


def test_a_binned_memo_is_in_no_project_count_and_no_tag(repo, wav):
    full_memo(repo, wav, "live.m4a", project="work")
    gone = full_memo(repo, wav, "gone.m4a", project="secret")

    services.delete_memo(repo, gone)

    assert services.projects(repo) == [("work", 1)]
    assert services.tags(repo) == [("release", 1)]
    assert [m.id for m in services.memos(repo, tag="release")] == [1]


def test_a_binned_memos_tasks_are_on_no_board_and_cannot_be_ticked_from_one(repo, wav):
    live = full_memo(repo, wav, "live.m4a")
    gone = full_memo(repo, wav, "gone.m4a")
    task = [t for t in repo.todos(include_done=True) if t.memo_id == gone]
    services.delete_memo(repo, gone)

    assert [r.memo_id for r in services.todo_rows(repo)] == [live]
    assert [r.memo_id for r in services.todo_rows(repo, memo_id=gone)] == []
    assert "memo 2" not in services.todos_text(repo)
    with pytest.raises(services.NotFound):
        services.set_todo_status(repo, task[0].id, "done")
    services.restore_memo(repo, gone)
    assert [r.memo_id for r in services.todo_rows(repo)] == [live, gone]


def test_a_binned_memos_voices_leave_the_pool_and_return_on_restore(repo, wav):
    memo_id = full_memo(repo, wav)
    assert list(repo.known_embeddings()) == ["Alice"]

    services.delete_memo(repo, memo_id)
    assert repo.known_embeddings() == {}

    services.restore_memo(repo, memo_id)
    assert list(repo.known_embeddings()) == ["Alice"]


def test_a_chat_about_binned_memos_is_listed_only_while_one_live_memo_is_in_it(repo, wav):
    first = full_memo(repo, wav, "a.m4a")
    second = full_memo(repo, wav, "b.m4a")
    both = services.start_chat(repo, [first, second])
    only_first = services.start_chat(repo, [first])

    services.delete_memo(repo, first)

    assert [c.conversation.id for c in services.conversations(repo)] == [both]
    assert services.require_conversation(repo, both).memo_ids == (second,)
    assert [c.conversation.id for c in services.conversations(repo, first)] == []

    services.restore_memo(repo, first)
    assert {c.conversation.id for c in services.conversations(repo)} == {both, only_first}
    assert services.require_conversation(repo, both).memo_ids == (first, second)


def test_chatting_in_a_thread_whose_every_memo_is_binned_points_at_restore(repo, wav):
    memo_id = full_memo(repo, wav)
    chat = services.start_chat(repo, [memo_id])
    services.delete_memo(repo, memo_id)

    with pytest.raises(services.NotFound, match=f"memo {memo_id} is in the bin"):
        services.chat(repo, chat, "what was decided?")


def test_a_memo_in_the_bin_is_listed_by_the_bin_with_the_disk_its_audio_holds(repo, wav):
    memo_id = full_memo(repo, wav, project="work")
    services.delete_memo(repo, memo_id)

    (row,) = services.bin_rows(repo)

    assert (row.id, row.title, row.project) == (memo_id, "standup.m4a", "work")
    assert row.size_bytes == wav.stat().st_size
    assert row.size.endswith("B")
    assert [r["audio_bytes"] for r in json.loads(services.bin_json(repo))] == [
        wav.stat().st_size
    ]
    assert services.bin_text(repo).split()[0] == str(memo_id)
    assert services.binned_count(repo) == 1


def test_the_bin_lists_the_memo_binned_last_first(repo, wav):
    first = full_memo(repo, wav, "a.m4a")
    second = full_memo(repo, wav, "b.m4a")
    services.delete_memo(repo, first)
    services.delete_memo(repo, second)

    assert [m.id for m in services.binned_memos(repo)] == [second, first]


# --- addressing a binned memo -----------------------------------------------

BY_ID = {
    "show": lambda repo, i: services.memo_heading(repo, i),
    "notes": lambda repo, i: services.notes(repo, i),
    "notes_json": lambda repo, i: services.notes_json(repo, i),
    "ask": lambda repo, i: services.ask(repo, i, "what?"),
    "extract": lambda repo, i: services.run_extraction(repo, i),
    "refine": lambda repo, i: services.refine_transcript(repo, i),
    "diarize": lambda repo, i: services.rediarize(repo, i),
    "title": lambda repo, i: services.rename_memo(repo, i, "new"),
    "move": lambda repo, i: services.move_memo(repo, i, "side"),
    "archive": lambda repo, i: services.archive_memo(repo, i),
    "unarchive": lambda repo, i: services.unarchive_memo(repo, i),
    "chat": lambda repo, i: services.start_chat(repo, [i]),
    "rename_speaker": lambda repo, i: services.rename_speaker(repo, i, "S1", "Bob"),
    "save_notes": lambda repo, i: services.save_notes(repo, i, "x"),
    "export_notes": lambda repo, i: services.export_notes(repo, i),
}


@pytest.mark.parametrize("name", sorted(BY_ID))
def test_addressing_a_binned_memo_says_where_it_is_and_how_to_get_it_back(repo, wav, name):
    memo_id = full_memo(repo, wav)
    services.delete_memo(repo, memo_id)

    with pytest.raises(services.NotFound) as raised:
        BY_ID[name](repo, memo_id)

    assert str(raised.value) == f"memo {memo_id} is in the bin — vtn restore {memo_id}"


def test_info_still_describes_a_binned_memo_and_says_it_is_in_the_bin(repo, wav):
    memo_id = full_memo(repo, wav)
    services.delete_memo(repo, memo_id)

    assert services.memo_info(repo, memo_id).binned is True
    assert f"in bin:   yes — vtn restore {memo_id}" in services.memo_info_text(repo, memo_id)


def test_info_on_a_live_memo_has_no_bin_line(repo, wav):
    memo_id = full_memo(repo, wav)

    assert "bin" not in services.memo_info_text(repo, memo_id)


# --- deleting for good ------------------------------------------------------


def test_deleting_for_good_works_on_a_memo_that_is_not_in_the_bin_and_on_one_that_is(repo, wav):
    live = full_memo(repo, wav, "a.m4a")
    other_wav = wav.parent / "other.wav"
    other_wav.write_bytes(b"RIFF")
    binned = full_memo(repo, other_wav, "b.m4a")
    services.delete_memo(repo, binned)

    services.purge_memo(repo, live)
    services.purge_memo(repo, binned)

    assert repo.memo(live) is None and repo.memo(binned) is None
    assert not wav.exists() and not other_wav.exists()
    assert repo.extraction(live) is None


def test_deleting_for_good_leaves_the_original_recording_alone(repo, tmp_path):
    original = tmp_path / "standup.m4a"
    original.write_bytes(b"original")
    converted = tmp_path / "standup.wav"
    converted.write_bytes(b"RIFF")
    memo_id = full_memo(repo, converted)

    services.purge_memo(repo, memo_id)

    assert original.read_bytes() == b"original"
    assert not converted.exists()


def test_emptying_the_bin_removes_every_binned_memo_and_no_live_one(repo, wav):
    live = full_memo(repo, wav, "live.m4a")
    first = full_memo(repo, wav, "a.m4a")
    second = full_memo(repo, wav, "b.m4a")
    services.delete_memo(repo, first)
    services.delete_memo(repo, second)

    assert services.empty_bin(repo).purged == 2

    assert repo.binned() == []
    assert repo.memo(live) is not None
    assert services.empty_bin(repo).purged == 0


# --- an old database --------------------------------------------------------


def test_a_database_from_before_the_bin_learns_the_column_and_keeps_its_memos(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE memos (
          id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
          duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          project TEXT NOT NULL DEFAULT 'other', notes_md TEXT, updated_at TEXT,
          recorded_at TEXT, archived_at TEXT, source_url TEXT
        );
        """
    )
    con.execute("INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')")
    con.commit()
    con.close()

    repo = Repository(path)

    assert repo.memo(1).deleted_at is None
    assert [m.id for m in repo.memos()] == [1]
    repo.set_binned(1, True)
    assert repo.memos() == []
    repo.close()


# --- importing something already in the bin ---------------------------------


def video(**overrides) -> YouTubeVideo:
    fields = dict(
        video_id="abc123",
        url="https://www.youtube.com/watch?v=abc123",
        title="How to think",
        channel="Some Channel",
        duration_s=60.0,
        uploaded_at=None,
        language="en",
        is_live=False,
        subtitles={},
        automatic_captions={},
    )
    fields.update(overrides)
    return YouTubeVideo(**fields)


def test_a_video_whose_memo_is_in_the_bin_is_pointed_at_restore_not_imported_again(repo):
    memo_id = repo.create_memo(
        filename="How to think", wav_path="", duration_s=60.0, language="en", segments=[],
        source_url="https://www.youtube.com/watch?v=abc123",
    )
    assert services.find_youtube_duplicate(repo, video()).id == memo_id
    services.delete_memo(repo, memo_id)

    with pytest.raises(services.NotFound, match=f"vtn restore {memo_id}"):
        services.find_youtube_duplicate(repo, video())


def test_a_recording_whose_memo_is_in_the_bin_is_pointed_at_restore(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(services.audio, "recorded_at", lambda _src: "2026-08-17T06:01:22Z")
    memo_id = repo.create_memo(
        filename="standup.m4a", wav_path="/tmp/x.wav", duration_s=1.0, language="en",
        segments=[], recorded_at="2026-08-17T06:01:22Z",
    )
    services.delete_memo(repo, memo_id)

    with pytest.raises(services.NotFound, match=f"memo {memo_id} is in the bin"):
        services.find_duplicate(repo, tmp_path / "standup.m4a")


# --- the command line -------------------------------------------------------


@pytest.fixture
def at_db(tmp_path, monkeypatch):
    """The real command line over a database of the test's own, opened afresh
    for every command the way each invocation does."""
    path = tmp_path / "cli.db"
    Repository(path).close()
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: Repository(path))

    def run(*argv: str) -> None:
        monkeypatch.setattr(sys, "argv", ["vtn", *argv])
        cli.main()

    return path, run


def seeded(path, wav) -> int:
    with Repository(path) as repo:
        return full_memo(repo, wav)


def test_delete_moves_a_memo_to_the_bin_and_says_how_to_undo_it(at_db, wav, capsys):
    path, run = at_db
    memo_id = seeded(path, wav)

    run("delete", str(memo_id))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f'moved "standup.m4a" to the bin — vtn restore {memo_id}\n'
    with Repository(path) as repo:
        assert repo.memo(memo_id).deleted_at is not None
    assert wav.exists()


def test_restore_brings_a_memo_back_and_a_second_restore_changes_nothing(at_db, wav, capsys):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))
    capsys.readouterr()

    run("restore", str(memo_id))
    run("restore", str(memo_id))

    assert capsys.readouterr().err.count(f"memo {memo_id} restored — standup.m4a") == 2
    with Repository(path) as repo:
        assert repo.memo(memo_id).deleted_at is None


def test_bin_lists_what_was_deleted_on_stdout_and_as_json(at_db, wav, capsys):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("bin")
    assert capsys.readouterr().err == "the bin is empty\n"
    run("delete", str(memo_id))
    capsys.readouterr()

    run("bin")
    listing = capsys.readouterr()
    run("bin", "--json")
    as_json = json.loads(capsys.readouterr().out)

    assert "standup.m4a" in listing.out and "work" in listing.out
    assert listing.err == ""
    assert as_json[0]["id"] == memo_id
    assert as_json[0]["audio_bytes"] == wav.stat().st_size
    assert as_json[0]["deleted_at"]


@pytest.mark.parametrize(
    "command",
    ["show", "notes", "ask", "extract", "refine", "diarize", "title", "move", "archive", "chat"],
)
def test_every_command_that_takes_a_memo_says_it_is_in_the_bin(at_db, wav, command):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))
    extra = {
        "ask": ["question"], "title": ["name"], "move": ["side"], "chat": ["what was decided?"],
    }.get(command, [])
    argv = [command, str(memo_id), *extra]

    with pytest.raises(SystemExit) as raised:
        run(*argv)

    assert str(raised.value) == f"memo {memo_id} is in the bin — vtn restore {memo_id}"


def test_info_describes_a_binned_memo(at_db, wav, capsys):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))
    capsys.readouterr()

    run("info", str(memo_id))

    assert f"in bin:   yes — vtn restore {memo_id}" in capsys.readouterr().out


def test_deleting_for_good_off_a_terminal_refuses_without_yes_and_removes_nothing(
    at_db, wav, monkeypatch
):
    path, run = at_db
    memo_id = seeded(path, wav)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("prompted off a terminal"))

    with pytest.raises(SystemExit) as raised:
        run("delete", str(memo_id), "--forever")

    assert "without --yes" in str(raised.value)
    with Repository(path) as repo:
        assert repo.memo(memo_id) is not None
    assert wav.exists()


def test_deleting_for_good_with_yes_removes_a_memo_whether_or_not_it_is_binned(
    at_db, wav, capsys, monkeypatch
):
    path, run = at_db
    memo_id = seeded(path, wav)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    run("delete", str(memo_id), "--forever", "--yes")

    assert capsys.readouterr().err == "deleted memo 1 — standup.m4a for good\n"
    with Repository(path) as repo:
        assert repo.memo(memo_id) is None
    assert not wav.exists()


def test_deleting_for_good_on_a_terminal_asks_and_a_no_keeps_the_memo(
    at_db, wav, capsys, monkeypatch
):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")

    run("delete", str(memo_id), "--forever")

    assert "kept" in capsys.readouterr().err
    with Repository(path) as repo:
        assert repo.memo(memo_id) is not None


def test_deleting_for_good_on_a_terminal_asks_and_a_yes_removes_it(at_db, wav, monkeypatch):
    path, run = at_db
    memo_id = seeded(path, wav)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "y")

    run("delete", str(memo_id), "--forever")

    with Repository(path) as repo:
        assert repo.memo(memo_id) is None


def test_emptying_the_bin_off_a_terminal_refuses_without_yes(at_db, wav, monkeypatch):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as raised:
        run("bin", "empty")

    assert "without --yes" in str(raised.value)
    with Repository(path) as repo:
        assert repo.memo(memo_id) is not None


def test_emptying_the_bin_names_how_many_go_and_removes_only_the_binned(
    at_db, wav, capsys, monkeypatch, tmp_path
):
    path, run = at_db
    gone = seeded(path, wav)
    other = tmp_path / "keep.wav"
    other.write_bytes(b"RIFF")
    with Repository(path) as repo:
        kept = full_memo(repo, other, "keep.m4a")
    run("delete", str(gone))
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "y")

    run("bin", "empty")

    err = capsys.readouterr().err
    assert "delete 1 memo in the bin for good" in err
    assert "emptied the bin — 1 memo deleted for good" in err
    with Repository(path) as repo:
        assert repo.memo(gone) is None
        assert repo.memo(kept) is not None


def test_emptying_an_empty_bin_says_so_and_asks_nothing(at_db, capsys, monkeypatch):
    _path, run = at_db
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    run("bin", "empty")

    assert capsys.readouterr().err == "the bin is already empty\n"


def test_list_shows_neither_a_binned_memo_nor_one_archived_before_it_was_binned(
    at_db, wav, capsys
):
    path, run = at_db
    memo_id = seeded(path, wav)
    run("archive", str(memo_id))
    run("delete", str(memo_id))
    capsys.readouterr()

    run("list")
    run("list", "--archived")
    run("todos")

    captured = capsys.readouterr()
    assert captured.out == ""


def test_the_help_for_delete_says_it_goes_to_the_bin(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["vtn", "delete", "--help"])

    with pytest.raises(SystemExit):
        cli.main()

    out = capsys.readouterr().out
    assert "bin" in out and "--forever" in out
