from voice_to_note.domain import Segment, Turn
from voice_to_note.transforms.speakers import assign_speakers


def seg(t0: int, t1: int) -> Segment:
    return Segment(t0_ms=t0, t1_ms=t1, text="x")


def test_speaker_with_most_overlap_wins():
    turns = [Turn(0, 600, "S1"), Turn(600, 2000, "S2")]
    (out,) = assign_speakers([seg(0, 1000)], turns)
    assert out.speaker == "S1"


def test_segment_spanning_two_turns_takes_bigger_overlap():
    turns = [Turn(0, 1000, "S1"), Turn(1000, 5000, "S2")]
    (out,) = assign_speakers([seg(900, 2000)], turns)
    assert out.speaker == "S2"


def test_segment_in_gap_falls_back_to_nearest_turn():
    turns = [Turn(0, 1000, "S1"), Turn(5000, 6000, "S2")]
    (out,) = assign_speakers([seg(1200, 1400)], turns)
    assert out.speaker == "S1"


def test_segment_in_gap_picks_nearest_boundary_either_side():
    turns = [Turn(0, 1000, "S1"), Turn(5000, 6000, "S2")]
    (out,) = assign_speakers([seg(4000, 4600)], turns)
    assert out.speaker == "S2"


def test_no_turns_leaves_speaker_unset():
    (out,) = assign_speakers([seg(0, 1000)], [])
    assert out.speaker is None


def test_input_segments_are_not_mutated():
    original = seg(0, 1000)
    assign_speakers([original], [Turn(0, 1000, "S1")])
    assert original.speaker is None
