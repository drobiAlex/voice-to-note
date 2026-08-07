import numpy as np
import pytest

from voice_to_note.transforms.speakers import (
    average_embedding,
    match_known_speakers,
    resolve_speaker_names,
)


def vec(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_cosine_match_above_threshold_assigns_name():
    matches = match_known_speakers({"S1": vec(1, 0, 0)}, {"Alice": [vec(0.9, 0.1, 0)]}, 0.5)
    assert matches["S1"].name == "Alice"
    assert matches["S1"].similarity > 0.5


def test_no_match_below_threshold():
    assert match_known_speakers({"S1": vec(1, 0, 0)}, {"Alice": [vec(0, 1, 0)]}, 0.5) == {}


def test_best_scoring_known_speaker_wins():
    pool = {"Alice": [vec(0, 1, 0)], "Bob": [vec(0.95, 0.05, 0)]}
    assert match_known_speakers({"S1": vec(1, 0, 0)}, pool, 0.5)["S1"].name == "Bob"


def test_multiple_stored_embeddings_pool_into_a_centroid():
    # neither stored vector alone clears the threshold; their centroid does
    pool = {"Alice": [vec(1, 1, 0), vec(1, -1, 0)]}
    assert match_known_speakers({"S1": vec(1, 0, 0)}, pool, 0.9)["S1"].name == "Alice"


def test_empty_pool_matches_nothing():
    assert match_known_speakers({"S1": vec(1, 0, 0)}, {}, 0.5) == {}


def test_manual_name_beats_auto_match():
    embs = {"S1": vec(1, 0, 0)}
    matches = match_known_speakers(embs, {"Alice": [vec(1, 0, 0)]}, 0.5)
    (speaker,) = resolve_speaker_names(["S1"], embs, matches, {"S1": "Bob"})
    assert speaker.name == "Bob"


def test_auto_match_fills_in_unnamed_labels():
    embs = {"S1": vec(1, 0, 0)}
    matches = match_known_speakers(embs, {"Alice": [vec(1, 0, 0)]}, 0.5)
    (speaker,) = resolve_speaker_names(["S1"], embs, matches, {})
    assert speaker.name == "Alice"


def test_label_without_match_or_manual_name_stays_unnamed():
    speakers = resolve_speaker_names(["S1", "S2"], {"S1": vec(1, 0, 0)}, {}, {})
    assert [s.name for s in speakers] == [None, None]
    assert speakers[1].embedding is None


def test_unnormalized_pool_embedding_still_matches():
    pool = {"Alice": [np.array([9.0, 0.0, 0.0], dtype=np.float32)]}
    match = match_known_speakers({"S1": np.array([2.0, 0.0, 0.0], dtype=np.float32)}, pool, 0.5)
    assert match["S1"].name == "Alice"
    assert match["S1"].similarity == pytest.approx(1.0)


def test_a_loud_embedding_cannot_fake_a_match():
    # long vector, wrong direction: only its length would clear the threshold
    embeddings = {"S1": np.array([0.6, 5.0, 0.0], dtype=np.float32)}
    assert match_known_speakers(embeddings, {"Alice": [vec(1, 0, 0)]}, 0.5) == {}


def test_one_loud_sample_does_not_dominate_a_centroid():
    pool = {"Alice": [vec(1, 0, 0) * 10, vec(0, 1, 0)]}
    assert match_known_speakers({"S1": vec(1, 0, 0)}, pool, 0.9) == {}


def test_average_embedding_is_unit_length():
    v = average_embedding([vec(1, 1, 0), vec(1, -1, 0)])
    assert np.isclose(np.linalg.norm(v), 1.0)
