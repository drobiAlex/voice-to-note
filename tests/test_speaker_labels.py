from voice_to_note.transforms.speakers import turns_from_clusters


def test_cluster_ids_become_s1_s2_by_first_appearance():
    turns = turns_from_clusters([(0.0, 1.0, 7), (1.0, 2.0, 3), (2.0, 3.0, 7)])
    assert [t.speaker for t in turns] == ["S1", "S2", "S1"]


def test_seconds_become_milliseconds():
    (turn,) = turns_from_clusters([(1.25, 3.5, 0)])
    assert (turn.start_ms, turn.end_ms) == (1250, 3500)


def test_no_clusters_gives_no_turns():
    assert turns_from_clusters([]) == []
