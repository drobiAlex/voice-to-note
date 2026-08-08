import json

import pytest
from conftest import seg, transcript

from voice_to_note.domain import Segment
from voice_to_note.transforms import refine


def ids(segments) -> list[int]:
    return [s.id for s in segments]


# --- splitting a transcript into repairable windows ---------------------


def test_a_short_transcript_is_one_window_with_nothing_around_it():
    chunks = refine.chunk_segments(transcript(5))

    assert len(chunks) == 1
    assert ids(chunks[0].targets) == [0, 1, 2, 3, 4]
    assert chunks[0].before == []
    assert chunks[0].after == []


def test_an_empty_transcript_has_nothing_to_repair():
    assert refine.chunk_segments([]) == []


def test_refining_lines_that_were_never_stored_is_refused():
    # repairs come back keyed by id; a line without one could never be filed
    unsaved = Segment(0, 900, "not written to the database yet", "S1")

    with pytest.raises(ValueError, match="id"):
        refine.chunk_segments([unsaved])


def test_a_transcript_of_exactly_one_window_is_not_split():
    chunks = refine.chunk_segments(transcript(refine.CHUNK_SIZE))

    assert len(chunks) == 1
    assert len(chunks[0].targets) == refine.CHUNK_SIZE


def test_one_segment_past_the_boundary_starts_a_second_window():
    n = refine.CHUNK_SIZE + 1
    chunks = refine.chunk_segments(transcript(n))

    assert len(chunks) == 2
    assert ids(chunks[1].targets) == [n - 1]


def test_every_segment_is_repaired_in_exactly_one_window():
    # a segment repaired twice, or not at all, is a silently corrupted transcript
    n = refine.CHUNK_SIZE * 3 + 7
    targeted = [i for chunk in refine.chunk_segments(transcript(n)) for i in ids(chunk.targets)]

    assert targeted == list(range(n))


def test_a_middle_window_can_read_the_lines_on_both_sides():
    chunks = refine.chunk_segments(transcript(refine.CHUNK_SIZE * 2))
    second = chunks[1]
    first_target = second.targets[0].id

    assert ids(second.before) == [
        first_target - refine.CONTEXT_SEGMENTS + i for i in range(refine.CONTEXT_SEGMENTS)
    ]
    assert second.after == []


def test_context_never_runs_past_the_ends_of_the_transcript():
    chunks = refine.chunk_segments(transcript(refine.CHUNK_SIZE * 2 + 1))

    assert chunks[0].before == []
    assert ids(chunks[0].after) == [refine.CHUNK_SIZE + i for i in range(refine.CONTEXT_SEGMENTS)]
    assert chunks[-1].after == []


# --- reading the reply ---------------------------------------------------


def reply(pairs: dict[int, object]) -> str:
    return json.dumps({"segments": [{"id": i, "text": t} for i, t in pairs.items()]})


def test_a_clean_reply_becomes_the_repaired_text_per_line():
    text = reply({0: "They're going to the meeting.", 1: "On Friday."})

    assert refine.parse_refinements(text, [0, 1]) == {
        0: "They're going to the meeting.",
        1: "On Friday.",
    }


def test_a_reply_wrapped_in_thinking_or_prose_is_still_read():
    # the local models narrate; parse_notes already learned this the hard way
    text = f"<think>the user wants JSON</think>\nSure:\n{reply({0: 'Fixed.'})}\nhope that helps"

    assert refine.parse_refinements(text, [0]) == {0: "Fixed."}


def test_a_reply_that_is_not_json_is_refused():
    with pytest.raises(ValueError):
        refine.parse_refinements("I could not repair that transcript.", [0])


def test_a_reply_that_drops_a_line_is_refused_by_id():
    # silently keeping the original would hide that the model ignored the line
    with pytest.raises(ValueError, match="7"):
        refine.parse_refinements(reply({6: "Fixed."}), [6, 7])


def test_a_reply_inventing_a_line_is_refused_by_id():
    with pytest.raises(ValueError, match="99"):
        refine.parse_refinements(reply({6: "Fixed.", 99: "Invented."}), [6])


def test_a_reply_whose_text_is_not_text_is_refused_by_id():
    with pytest.raises(ValueError, match="6"):
        refine.parse_refinements(reply({6: 123}), [6])


def test_a_reply_entry_with_no_id_at_all_is_refused():
    # text with nothing to attach it to cannot be filed against a line
    with pytest.raises(ValueError):
        refine.parse_refinements('{"segments": [{"text": "Fixed."}]}', [0])


def test_a_reply_repairing_the_same_line_twice_is_refused():
    # last-wins would quietly pick one of two different repairs for one line
    text = '{"segments": [{"id": 6, "text": "A"}, {"id": 6, "text": "B"}]}'

    with pytest.raises(ValueError, match="6"):
        refine.parse_refinements(text, [6])


def test_a_reply_whose_id_is_text_is_refused_as_an_id_problem():
    # "6" never matches 6, so without this the reply reads as a missing line
    with pytest.raises(ValueError, match="'6'"):
        refine.parse_refinements('{"segments": [{"id": "6", "text": "A"}]}', [6])


def test_a_reply_whose_id_is_a_flag_is_refused():
    # true is an int in Python, so unguarded it would file against segment 1
    with pytest.raises(ValueError):
        refine.parse_refinements('{"segments": [{"id": true, "text": "A"}]}', [1])


def test_a_reply_whose_id_is_not_even_a_number_is_refused():
    with pytest.raises(ValueError):
        refine.parse_refinements('{"segments": [{"id": [6], "text": "A"}]}', [6])


def test_a_reply_without_the_segments_list_is_refused():
    with pytest.raises(ValueError, match="segments"):
        refine.parse_refinements('{"lines": []}', [0])


# --- combining the windows ------------------------------------------------


def test_repairs_from_separate_windows_are_collected_together():
    first = refine.Chunk(before=[], targets=[seg(0), seg(1)], after=[seg(2)])
    second = refine.Chunk(before=[seg(1)], targets=[seg(2)], after=[])

    merged = refine.merge_refinements([first, second], [{0: "A", 1: "B"}, {2: "C"}])

    assert merged == {0: "A", 1: "B", 2: "C"}


def test_a_line_repaired_twice_keeps_the_version_that_had_context_both_sides():
    # at the edge of a window the model is guessing at half the sentence
    at_edge = refine.Chunk(before=[seg(2)], targets=[seg(3), seg(4), seg(5)], after=[])
    interior = refine.Chunk(before=[seg(4)], targets=[seg(5), seg(6)], after=[seg(7)])

    merged = refine.merge_refinements(
        [at_edge, interior], [{3: "A", 4: "B", 5: "edge guess"}, {5: "informed", 6: "D"}]
    )

    assert merged[5] == "informed"


def test_a_line_placed_well_in_both_windows_settles_on_the_first():
    # neither repair is better placed, so the choice is arbitrary — but it has
    # to be the same arbitrary choice every run
    first = refine.Chunk(before=[seg(2)], targets=[seg(3), seg(4)], after=[seg(5)])
    second = refine.Chunk(before=[seg(3)], targets=[seg(4), seg(5)], after=[seg(6)])

    merged = refine.merge_refinements(
        [first, second], [{3: "A", 4: "first"}, {4: "second", 5: "B"}]
    )

    assert merged[4] == "first"


def test_windows_and_repairs_that_do_not_line_up_are_refused():
    # they are matched by position, so a mismatch would drop a whole window's
    # repairs without saying so — the one outcome this module exists to prevent
    chunk = refine.Chunk(before=[], targets=[seg(0)], after=[])

    with pytest.raises(ValueError):
        refine.merge_refinements([chunk], [{0: "A"}, {1: "B"}])


# --- refusing a rewrite that is not a repair -------------------------------


def test_a_small_correction_is_accepted():
    original = seg(0, "so their going to ship the thing on friday")
    repaired = {0: "So they're going to ship the thing on Friday."}

    text, flagged = refine.accept_repairs([original], repaired)

    assert text[0] == "So they're going to ship the thing on Friday."
    assert flagged == []


def test_a_line_rewritten_into_something_else_keeps_the_original():
    original = seg(0, "we should ship the release on friday afternoon")
    repaired = {0: "The cat sat upon the mat in perfect silence, waiting."}

    text, flagged = refine.accept_repairs([original], repaired)

    assert text[0] == "we should ship the release on friday afternoon"
    assert flagged == [0]


def test_a_line_the_model_padded_out_keeps_the_original():
    # same words, twice the length: the model expanded rather than repaired
    original = seg(0, "we should ship the release on friday afternoon")
    repaired = {0: "we should ship the release on friday afternoon " * 2}

    text, flagged = refine.accept_repairs([original], repaired)

    assert text[0] == "we should ship the release on friday afternoon"
    assert flagged == [0]


def test_a_line_the_model_truncated_keeps_the_original():
    original = seg(0, "we should ship the release on friday afternoon")
    repaired = {0: "we should ship"}

    text, flagged = refine.accept_repairs([original], repaired)

    assert text[0] == "we should ship the release on friday afternoon"
    assert flagged == [0]


def test_a_line_not_yet_stored_has_nothing_to_key_a_repair_to():
    unsaved = Segment(0, 900, "not written to the database yet", "S1")

    text, flagged = refine.accept_repairs([unsaved, seg(1, "second line")], {1: "Second line."})

    assert text == {1: "Second line."}
    assert flagged == []


def test_an_empty_line_is_neither_crashed_on_nor_filled_in():
    # a segment whose text is empty divides the length comparison by zero, and
    # is exactly the line a model is most tempted to invent something for
    empty = Segment(0, 900, "", "S1", 0)

    text, flagged = refine.accept_repairs([empty], {0: "Something nobody said."})

    assert text[0] == ""
    assert flagged == [0]


def test_lines_nobody_repaired_come_back_unchanged():
    segments = [seg(0, "first line"), seg(1, "second line")]

    text, flagged = refine.accept_repairs(segments, {1: "Second line."})

    assert text == {0: "first line", 1: "Second line."}
    assert flagged == []


def test_the_reply_schema_requires_an_id_and_text_on_every_entry():
    # the local model is held to this shape while decoding, the way notes are
    entry = refine.REFINE_SCHEMA["properties"]["segments"]["items"]

    assert refine.REFINE_SCHEMA["required"] == ["segments"]
    assert entry["required"] == ["id", "text"]
    assert entry["properties"]["id"]["type"] == "integer"
    assert entry["properties"]["text"]["type"] == "string"
