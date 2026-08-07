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


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "test.db")
    yield r
    r.close()


def make_memo(repo, *, segments=(), speakers=(), filename="memo.m4a"):
    return repo.create_memo(
        filename=filename,
        wav_path=f"/tmp/{filename}.wav",
        duration_s=12.5,
        language="en",
        segments=list(segments),
        speakers=list(speakers),
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
