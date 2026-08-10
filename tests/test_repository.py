import inspect
import re
import sqlite3

import numpy as np
import pytest

from voice_to_note.domain import Segment, Speaker
from voice_to_note.storage.repository import Repository

PRE_EMBEDDING_SCHEMA = """
CREATE TABLE memos (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
  duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE speakers (
  id INTEGER PRIMARY KEY, memo_id INTEGER NOT NULL, label TEXT NOT NULL,
  name TEXT, UNIQUE(memo_id, label)
);
"""


PRE_REFINEMENT_SCHEMA = """
CREATE TABLE memos (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
  duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE segments (
  id INTEGER PRIMARY KEY, memo_id INTEGER NOT NULL, t0_ms INTEGER NOT NULL,
  t1_ms INTEGER NOT NULL, text TEXT NOT NULL, speaker TEXT
);
"""


PRE_PROJECT_SCHEMA = """
CREATE TABLE memos (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
  duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE segments (
  id INTEGER PRIMARY KEY, memo_id INTEGER NOT NULL, t0_ms INTEGER NOT NULL,
  t1_ms INTEGER NOT NULL, text TEXT NOT NULL, speaker TEXT, refined_text TEXT
);
"""


PRE_UPDATED_SCHEMA = """
CREATE TABLE memos (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
  duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  project TEXT NOT NULL DEFAULT 'other', notes_md TEXT
);
"""


PRE_NOTES_SCHEMA = """
CREATE TABLE memos (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, wav_path TEXT NOT NULL,
  duration_s REAL, language TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  project TEXT NOT NULL DEFAULT 'other'
);
CREATE TABLE extractions (
  id INTEGER PRIMARY KEY, memo_id INTEGER NOT NULL, backend TEXT NOT NULL,
  json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def make_memo(repo, *, segments=(), speakers=(), filename="memo.m4a", **kwargs):
    return repo.create_memo(
        filename=filename,
        wav_path=f"/tmp/{filename}.wav",
        duration_s=12.5,
        language="en",
        segments=list(segments),
        speakers=list(speakers),
        **kwargs,
    )


def test_memo_and_segments_round_trip(repo):
    memo_id = make_memo(
        repo,
        segments=[
            Segment(0, 1000, "Hello", speaker="S1"),
            Segment(1000, 2000, "there", speaker="S2"),
        ],
    )
    memo = repo.memo(memo_id)
    assert (memo.filename, memo.language, memo.duration_s) == ("memo.m4a", "en", 12.5)
    assert memo.status == "transcribed"
    segs = repo.segments(memo_id)
    assert [(s.t0_ms, s.t1_ms, s.text, s.speaker) for s in segs] == [
        (0, 1000, "Hello", "S1"),
        (1000, 2000, "there", "S2"),
    ]
    assert all(s.id is not None for s in segs)


def test_segments_come_back_in_time_order(repo):
    memo_id = make_memo(
        repo, segments=[Segment(5000, 6000, "later"), Segment(0, 1000, "earlier")]
    )
    assert [s.text for s in repo.segments(memo_id)] == ["earlier", "later"]


def test_unknown_memo_is_none(repo):
    assert repo.memo(999) is None
    assert repo.memos() == []


def test_a_repository_knows_which_database_it_opened(tmp_path):
    # work happening off the main thread has to open its own connection to the
    # same file, and it can only do that if the file is still known
    repo = Repository(tmp_path / "memos.db")

    assert repo.path == tmp_path / "memos.db"
    repo.close()


def test_a_repository_block_hands_back_the_repository(tmp_path):
    with Repository(tmp_path / "block.db") as repo:
        memo_id = make_memo(repo)
        assert repo.memo(memo_id).filename == "memo.m4a"


def test_leaving_a_repository_block_closes_the_database(tmp_path):
    with Repository(tmp_path / "block.db") as repo:
        memo_id = make_memo(repo)

    with pytest.raises(sqlite3.ProgrammingError):
        repo.memo(memo_id)


def test_speaker_embeddings_round_trip_as_float32(repo):
    emb = np.array([0.5, -0.25, 0.125], dtype=np.float32)
    make_memo(repo, speakers=[Speaker("S1", "Alice", emb)])
    (stored,) = repo.known_embeddings()["Alice"]
    assert stored.dtype == np.float32
    assert np.array_equal(stored, emb)


def test_unnamed_speakers_stay_out_of_the_known_pool(repo):
    emb = np.array([1.0, 0.0], dtype=np.float32)
    make_memo(repo, speakers=[Speaker("S1", None, emb), Speaker("S2", "Bob", emb)])
    assert list(repo.known_embeddings()) == ["Bob"]


def test_known_embeddings_can_exclude_one_memo(repo):
    emb = np.array([1.0, 0.0], dtype=np.float32)
    memo_id = make_memo(repo, speakers=[Speaker("S1", "Alice", emb)])
    make_memo(repo, speakers=[Speaker("S1", "Bob", emb)], filename="other.m4a")
    assert list(repo.known_embeddings(exclude_memo_id=memo_id)) == ["Bob"]


def test_rename_updates_the_speaker_name(repo):
    memo_id = make_memo(repo, speakers=[Speaker("S1", None, None)])
    assert repo.rename_speaker(memo_id, "S1", "Alice") is True
    assert repo.display_names(memo_id) == {"S1": "Alice"}
    assert repo.named_speakers(memo_id) == {"S1": "Alice"}


def test_rename_reports_missing_speaker(repo):
    memo_id = make_memo(repo, speakers=[Speaker("S1", None, None)])
    assert repo.rename_speaker(memo_id, "S9", "Alice") is False


def test_display_names_fall_back_to_the_label(repo):
    memo_id = make_memo(repo, speakers=[Speaker("S1", None, None), Speaker("S2", "Bob", None)])
    assert repo.display_names(memo_id) == {"S1": "S1", "S2": "Bob"}
    assert repo.named_speakers(memo_id) == {"S2": "Bob"}


def test_rediarization_replaces_speakers_and_segment_labels(repo):
    memo_id = make_memo(
        repo,
        segments=[Segment(0, 1000, "Hello", speaker="S1")],
        speakers=[Speaker("S1", "Alice", None), Speaker("S2", None, None)],
    )
    (seg,) = repo.segments(memo_id)
    repo.save_diarization(
        memo_id,
        [Segment(seg.t0_ms, seg.t1_ms, seg.text, speaker="S2", id=seg.id)],
        [Speaker("S2", "Alice", None)],
    )
    assert [s.speaker for s in repo.segments(memo_id)] == ["S2"]
    assert repo.display_names(memo_id) == {"S2": "Alice"}


def test_extraction_replaces_the_previous_one(repo):
    memo_id = make_memo(repo)
    repo.save_extraction(memo_id, "claude", {"title": "first"})
    repo.save_extraction(memo_id, "ollama/qwen3:8b", {"title": "second"})
    # a leftover row would be the one read back, since the older row comes first
    stored = repo.extraction(memo_id)
    assert (stored.backend, stored.data["title"]) == ("ollama/qwen3:8b", "second")
    assert stored.created_at


def test_extraction_marks_the_memo_extracted(repo):
    memo_id = make_memo(repo)
    assert repo.extraction(memo_id) is None
    repo.save_extraction(memo_id, "claude", {"title": "x"})
    assert repo.memo(memo_id).status == "extracted"


def test_extraction_preserves_non_ascii(repo):
    memo_id = make_memo(repo)
    repo.save_extraction(memo_id, "claude", {"title": "Зустріч"})
    assert repo.extraction(memo_id).data["title"] == "Зустріч"


def test_opening_an_existing_database_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    first = Repository(path)
    memo_id = make_memo(first, segments=[Segment(0, 1000, "Hello")])
    first.close()
    second = Repository(path)
    assert [s.text for s in second.segments(memo_id)] == ["Hello"]
    second.close()


def test_pre_embedding_database_gains_the_embedding_column(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_EMBEDDING_SCHEMA)
    con.execute(
        "INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')"
    )
    con.execute("INSERT INTO speakers (memo_id, label, name) VALUES (1, 'S1', 'Alice')")
    con.commit()
    con.close()

    repo = Repository(path)
    assert repo.display_names(1) == {"S1": "Alice"}
    assert repo.known_embeddings() == {}

    emb = np.array([0.5, -0.5], dtype=np.float32)
    repo.save_diarization(1, [], [Speaker("S1", "Alice", emb)])
    assert np.array_equal(repo.known_embeddings()["Alice"][0], emb)
    repo.close()


def test_a_transcript_starts_out_with_nothing_refined(repo):
    memo_id = make_memo(repo, segments=[Segment(0, 1000, "as transcribed")])

    assert repo.segments(memo_id)[0].refined_text is None


def test_a_refinement_is_stored_beside_the_words_that_were_heard(repo):
    # the recording is the record: a repair never overwrites what was transcribed
    memo_id = make_memo(repo, segments=[Segment(0, 1000, "so their going")])
    seg_id = repo.segments(memo_id)[0].id

    repo.update_refinements(memo_id, {seg_id: "So they're going"})

    stored = repo.segments(memo_id)[0]
    assert stored.text == "so their going"
    assert stored.refined_text == "So they're going"


def test_refining_a_memo_again_replaces_the_earlier_pass(repo):
    # a second pass is the whole refinement of that memo, not an addition to it
    memo_id = make_memo(
        repo, segments=[Segment(0, 1000, "first line"), Segment(1000, 2000, "second line")]
    )
    first, second = repo.segments(memo_id)
    repo.update_refinements(memo_id, {first.id: "First line.", second.id: "Second line."})

    repo.update_refinements(memo_id, {second.id: "Second line!"})

    assert [s.refined_text for s in repo.segments(memo_id)] == [None, "Second line!"]


def test_a_database_from_before_refinement_gains_the_column(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_REFINEMENT_SCHEMA)
    con.execute("INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')")
    con.execute(
        "INSERT INTO segments (memo_id, t0_ms, t1_ms, text, speaker)"
        " VALUES (1, 0, 1000, 'as first transcribed', 'S1')"
    )
    con.commit()
    con.close()

    repo = Repository(path)
    stored = repo.segments(1)
    assert [s.text for s in stored] == ["as first transcribed"]
    assert stored[0].refined_text is None

    repo.update_refinements(1, {stored[0].id: "As first transcribed."})
    assert repo.segments(1)[0].refined_text == "As first transcribed."
    repo.close()


def test_a_memo_is_filed_under_the_project_it_was_given(repo):
    memo_id = repo.create_memo(
        filename="standup.m4a", wav_path="/tmp/a.wav", duration_s=1.0,
        language="en", segments=[], project="work",
    )

    assert repo.memo(memo_id).project == "work"


def test_a_memo_filed_nowhere_lands_in_other(repo):
    # every memo belongs somewhere, so there is always a project to group by
    assert repo.memo(make_memo(repo)).project == "other"


def test_memos_can_be_listed_one_project_at_a_time(repo):
    make_memo(repo, filename="a.m4a", project="work")
    make_memo(repo, filename="b.m4a", project="personal")
    make_memo(repo, filename="c.m4a", project="work")

    assert [m.filename for m in repo.memos(project="work")] == ["c.m4a", "a.m4a"]
    assert [m.filename for m in repo.memos()] == ["c.m4a", "b.m4a", "a.m4a"]


def test_the_project_list_counts_what_is_in_each(repo):
    # the sidebar needs the names and how much is in them, in one query
    make_memo(repo, filename="a.m4a", project="work")
    make_memo(repo, filename="b.m4a", project="personal")
    make_memo(repo, filename="c.m4a", project="work")

    assert repo.projects() == [("personal", 1), ("work", 2)]


def test_a_memo_can_be_moved_to_another_project(repo):
    memo_id = make_memo(repo, project="work")

    repo.set_project(memo_id, "side")

    assert repo.memo(memo_id).project == "side"


def test_a_database_from_before_projects_files_its_memos_under_other(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_PROJECT_SCHEMA)
    con.execute("INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')")
    con.commit()
    con.close()

    repo = Repository(path)
    memo = repo.memo(1)
    assert (memo.filename, memo.project) == ("old.m4a", "other")
    assert repo.projects() == [("other", 1)]
    repo.close()


def test_a_memo_starts_with_no_note_of_its_own(repo):
    assert repo.notes_md(make_memo(repo)) is None


def test_an_edited_note_is_stored_and_read_back(repo):
    memo_id = make_memo(repo)

    repo.save_notes_md(memo_id, "# My own words")

    assert repo.notes_md(memo_id) == "# My own words"


def test_editing_a_note_leaves_the_extraction_untouched(repo):
    # the model's structured output is the record; the edit is a layer over it
    memo_id = make_memo(repo)
    repo.save_extraction(memo_id, "claude", {"title": "As extracted"})

    repo.save_notes_md(memo_id, "# My own words")

    assert repo.extraction(memo_id).data == {"title": "As extracted"}


def test_an_edited_note_can_be_taken_back_off(repo):
    memo_id = make_memo(repo)
    repo.save_notes_md(memo_id, "# My own words")

    repo.save_notes_md(memo_id, None)

    assert repo.notes_md(memo_id) is None


def test_a_database_from_before_note_editing_gains_the_column(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_NOTES_SCHEMA)
    con.execute("INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')")
    con.commit()
    con.close()

    repo = Repository(path)
    assert repo.memo(1).filename == "old.m4a"
    assert repo.notes_md(1) is None

    repo.save_notes_md(1, "# Written later")
    assert repo.notes_md(1) == "# Written later"
    repo.close()


def test_a_forced_extraction_clears_the_edit_along_with_it(repo):
    # one transaction: the new notes land and the old edit goes, or neither does
    memo_id = make_memo(repo)
    repo.save_notes_md(memo_id, "# My own words")

    repo.save_extraction(memo_id, "claude", {"title": "Fresh"}, clear_edited=True)

    assert repo.notes_md(memo_id) is None
    assert repo.extraction(memo_id).data == {"title": "Fresh"}


def test_an_ordinary_extraction_leaves_an_edit_where_it_is(repo):
    memo_id = make_memo(repo)
    repo.save_notes_md(memo_id, "# My own words")

    repo.save_extraction(memo_id, "claude", {"title": "Fresh"})

    assert repo.notes_md(memo_id) == "# My own words"


def test_the_migration_accounts_for_every_column_it_adds(repo):
    # four clauses have shipped and the docstring fell behind on the fourth;
    # this fails the next time a column is added without a word about it
    source = inspect.getsource(Repository._migrate)
    documented = Repository._migrate.__doc__ or ""

    assert [c for c in re.findall(r"ADD COLUMN (\w+)", source) if c not in documented] == []


# --- finding memos by the tags their notes carry --------------------------


def tag_memo(repo, filename: str, tags: list[str], **kwargs) -> int:
    """A stored memo whose extraction carries these tags."""
    memo_id = make_memo(repo, filename=filename, **kwargs)
    repo.save_extraction(memo_id, "claude", {"title": filename, "tags": tags})
    return memo_id


def test_memos_can_be_found_by_a_tag_their_notes_carry(repo):
    wanted = tag_memo(repo, "a.m4a", ["release"])
    tag_memo(repo, "b.m4a", ["hiring"])

    assert [m.id for m in repo.memos(tag="release")] == [wanted]


def test_a_tag_search_ignores_the_case_the_tag_was_written_in(repo):
    memo_id = tag_memo(repo, "a.m4a", ["Release"])

    assert [m.id for m in repo.memos(tag="RELEASE")] == [memo_id]


def test_a_tag_search_matches_whole_tags_and_not_parts_of_them(repo):
    # berlin and berlinale are two subjects, not one of them a prefix
    tag_memo(repo, "a.m4a", ["berlinale"])

    assert repo.memos(tag="berlin") == []


def test_a_memo_nobody_extracted_carries_no_tags_to_be_found_by(repo):
    make_memo(repo)

    assert repo.memos(tag="release") == []


def test_a_tag_search_can_be_narrowed_to_one_project(repo):
    work = tag_memo(repo, "a.m4a", ["release"], project="work")
    tag_memo(repo, "b.m4a", ["release"], project="personal")

    assert [m.id for m in repo.memos(project="work", tag="release")] == [work]


def test_tagged_memos_come_back_newest_first_like_every_other_listing(repo):
    older = tag_memo(repo, "a.m4a", ["release"])
    newer = tag_memo(repo, "b.m4a", ["release"])

    assert [m.id for m in repo.memos(tag="release")] == [newer, older]


def test_a_memo_carrying_one_tag_twice_is_still_one_memo(repo):
    memo_id = tag_memo(repo, "a.m4a", ["release", "release"])

    assert [m.id for m in repo.memos(tag="release")] == [memo_id]


def test_writing_a_note_by_hand_leaves_the_tags_where_they_were(repo):
    # tags are the extraction's; an edit replaces how a memo reads, not what it
    # was filed under
    memo_id = tag_memo(repo, "a.m4a", ["release"])
    repo.save_notes_md(memo_id, "# My own words, no tags anywhere in them")

    assert [m.id for m in repo.memos(tag="release")] == [memo_id]


# --- when a memo was last changed -----------------------------------------


def test_a_memo_nobody_has_changed_has_no_update_time(repo):
    # NULL is not "changed when it was made": it says nothing has happened since
    memo_id = make_memo(repo)

    assert repo.memo(memo_id).updated_at is None


def test_moving_a_memo_marks_when_it_changed(repo):
    memo_id = make_memo(repo)

    repo.set_project(memo_id, "work")

    assert repo.memo(memo_id).updated_at is not None


def test_refiling_a_whole_project_marks_every_memo_it_carried(repo):
    first = make_memo(repo, filename="a.m4a", project="work")
    second = make_memo(repo, filename="b.m4a", project="work")

    repo.refile_project("work", "client")

    assert repo.memo(first).updated_at is not None
    assert repo.memo(second).updated_at is not None


def test_editing_the_notes_marks_when_the_memo_changed(repo):
    memo_id = make_memo(repo)

    repo.save_notes_md(memo_id, "# My own words")

    assert repo.memo(memo_id).updated_at is not None


def test_storing_an_extraction_marks_when_the_memo_changed(repo):
    memo_id = make_memo(repo)

    repo.save_extraction(memo_id, "claude", {"title": "Sprint sync"})

    assert repo.memo(memo_id).updated_at is not None


def test_naming_a_speaker_marks_when_the_memo_changed(repo):
    memo_id = make_memo(repo, speakers=[Speaker("S1")])

    repo.rename_speaker(memo_id, "S1", "Alice")

    assert repo.memo(memo_id).updated_at is not None


def test_repairing_the_transcript_marks_when_the_memo_changed(repo):
    memo_id = make_memo(repo, segments=[Segment(0, 900, "helo")])
    (stored,) = repo.segments(memo_id)

    repo.update_refinements(memo_id, {stored.id: "hello"})

    assert repo.memo(memo_id).updated_at is not None


def test_a_rename_that_found_no_such_speaker_marks_nothing(repo):
    # the memo reads exactly as it did, so recording a change would be a lie
    memo_id = make_memo(repo, speakers=[Speaker("S1")])

    repo.rename_speaker(memo_id, "S9", "Nobody")

    assert repo.memo(memo_id).updated_at is None


def test_a_database_from_before_change_times_gains_the_column(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_UPDATED_SCHEMA)
    con.execute("INSERT INTO memos (filename, wav_path) VALUES ('old.m4a', '/tmp/old.wav')")
    con.commit()
    con.close()

    repo = Repository(path)
    assert repo.memo(1).updated_at is None

    repo.set_project(1, "work")
    assert repo.memo(1).updated_at is not None
    repo.close()


def test_running_speaker_detection_again_marks_when_the_memo_changed(repo):
    memo_id = make_memo(repo, segments=[Segment(0, 900, "hi")], speakers=[Speaker("S1")])
    (stored,) = repo.segments(memo_id)

    repo.save_diarization(
        memo_id, [Segment(0, 900, "hi", "S2", stored.id)], [Speaker("S2")]
    )

    assert repo.memo(memo_id).updated_at is not None


# --- what a listing puts beside every memo --------------------------------


def test_a_listing_carries_the_voices_repairs_and_edits_of_every_memo(repo):
    plain = make_memo(repo, filename="plain.m4a", speakers=[Speaker("S1")])
    marked = make_memo(
        repo,
        filename="marked.m4a",
        segments=[Segment(0, 900, "helo")],
        speakers=[Speaker("S1"), Speaker("S2")],
    )
    (stored,) = repo.segments(marked)
    repo.update_refinements(marked, {stored.id: "hello"})
    repo.save_notes_md(marked, "# My own words")

    listed = {listing.memo.id: listing for listing in repo.memo_listings()}

    assert (listed[marked].speakers, listed[marked].refined, listed[marked].edited) == (
        2,
        True,
        True,
    )
    assert (listed[plain].speakers, listed[plain].refined, listed[plain].edited) == (
        1,
        False,
        False,
    )


def test_a_listing_narrows_to_a_project_or_a_tag_like_the_plain_memo_list(repo):
    tagged = tag_memo(repo, "a.m4a", ["release"], project="work")
    tag_memo(repo, "b.m4a", ["hiring"], project="work")
    make_memo(repo, filename="c.m4a", project="personal")

    listed = repo.memo_listings(project="work")

    assert [listing.memo.filename for listing in listed] == ["b.m4a", "a.m4a"]
    assert [listing.memo.id for listing in repo.memo_listings(tag="release")] == [tagged]


def test_a_listing_stops_calling_a_memo_edited_once_its_note_is_cleared(repo):
    # a forced re-extraction takes the hand-written note with it, and the list
    # would go on marking the memo as edited over notes nobody wrote
    memo_id = make_memo(repo)
    repo.save_notes_md(memo_id, "# My own words")

    repo.save_extraction(memo_id, "claude", {"title": "A"}, clear_edited=True)

    (listing,) = repo.memo_listings()
    assert listing.edited is False
    assert listing.refined is False


def test_a_listing_costs_one_query_however_many_memos_it_carries(repo):
    # the whole list is drawn at once, so a query per row is paid on every
    # reload of every project
    for i in range(4):
        make_memo(repo, filename=f"memo{i}.m4a", speakers=[Speaker("S1")])
    statements: list[str] = []
    repo.con.set_trace_callback(statements.append)

    listed = repo.memo_listings()

    repo.con.set_trace_callback(None)
    assert len(listed) == 4
    assert len(statements) == 1
