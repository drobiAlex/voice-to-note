from voice_to_note.domain import Segment
from voice_to_note.transforms.segments import (
    display_name,
    display_text,
    fmt_ts,
    transcript_text,
)


def test_display_name_prefers_the_assigned_name():
    assert display_name("S1", {"S1": "Alice"}) == "Alice"


def test_display_name_falls_back_to_the_label():
    assert display_name("S1", {}) == "S1"


def test_display_name_of_an_unassigned_speaker_is_unknown():
    assert display_name(None, {}) == "Unknown"


def test_minutes_do_not_wrap_into_hours():
    assert fmt_ts(65 * 60 * 1000) == "65:00"


def test_consecutive_same_speaker_segments_merge_into_one_line():
    segs = [
        Segment(0, 1000, "Hello there", speaker="S1"),
        Segment(1000, 2000, "how are you", speaker="S1"),
        Segment(2000, 3000, "Fine", speaker="S2"),
    ]
    assert transcript_text(segs, {"S1": "S1", "S2": "S2"}) == (
        "[00:00] S1: Hello there how are you\n[00:02] S2: Fine"
    )


def test_line_timestamp_is_the_start_of_the_run():
    segs = [
        Segment(0, 1000, "a", speaker="S1"),
        Segment(65_000, 66_000, "b", speaker="S2"),
        Segment(66_000, 67_000, "c", speaker="S2"),
    ]
    assert transcript_text(segs, {}).splitlines()[1].startswith("[01:05] S2: b c")


def test_speaker_names_replace_labels():
    segs = [Segment(0, 1000, "Hi", speaker="S1")]
    assert transcript_text(segs, {"S1": "Alice"}) == "[00:00] Alice: Hi"


def test_same_person_under_two_labels_still_merges():
    segs = [
        Segment(0, 1000, "Hi", speaker="S1"),
        Segment(1000, 2000, "again", speaker="S2"),
    ]
    assert transcript_text(segs, {"S1": "Alice", "S2": "Alice"}) == "[00:00] Alice: Hi again"


def test_missing_speaker_renders_as_unknown():
    segs = [Segment(0, 1000, "Hi", speaker=None)]
    assert transcript_text(segs, {}) == "[00:00] Unknown: Hi"


def test_no_segments_gives_empty_transcript():
    assert transcript_text([], {}) == ""


def test_the_line_to_read_is_the_repair_when_there_is_one():
    heard = Segment(0, 1000, "so their going", refined_text="So they're going")

    assert display_text(heard) == "So they're going"


def test_the_line_to_read_falls_back_to_what_was_transcribed():
    assert display_text(Segment(0, 1000, "as heard")) == "as heard"


def test_the_original_wording_can_always_be_asked_for():
    # the recording is the record: a repair never hides what was transcribed
    heard = Segment(0, 1000, "so their going", refined_text="So they're going")

    assert display_text(heard, raw=True) == "so their going"


def test_a_transcript_reads_the_repairs_where_they_exist():
    segs = [
        Segment(0, 1000, "hi their", speaker="S1", refined_text="Hi there"),
        Segment(1000, 2000, "ok", speaker="S1"),
    ]

    assert transcript_text(segs, {}) == "[00:00] S1: Hi there ok"


def test_a_transcript_can_be_asked_for_the_original_wording():
    segs = [Segment(0, 1000, "hi their", speaker="S1", refined_text="Hi there")]

    assert transcript_text(segs, {}, raw=True) == "[00:00] S1: hi their"
