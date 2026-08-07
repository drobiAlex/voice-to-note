from voice_to_note.domain import Turn
from voice_to_note.transforms.speakers import select_fingerprint_turns

TEN_MINUTES = 10 * 60 * 16000


def turn(start_ms: int, end_ms: int) -> Turn:
    return Turn(start_ms, end_ms, "S1")


def test_longest_turns_are_embedded_first():
    turns = [turn(0, 1000), turn(2000, 6000), turn(7000, 9000)]
    chosen = select_fingerprint_turns(turns, TEN_MINUTES)
    assert [t.start_ms for t in chosen] == [2000, 7000, 0]


def test_selection_stops_once_the_budget_is_spent():
    turns = [turn(i * 20_000, i * 20_000 + 20_000) for i in range(5)]
    assert len(select_fingerprint_turns(turns, TEN_MINUTES)) == 2


def test_turns_too_short_to_embed_are_dropped():
    turns = [turn(0, 400), turn(1000, 2000)]
    assert [t.start_ms for t in select_fingerprint_turns(turns, TEN_MINUTES)] == [1000]


def test_a_turn_that_cannot_be_embedded_does_not_consume_the_budget():
    # the long turn runs off the end of the audio and gets skipped; the two
    # real turns must still get the whole budget
    audio_samples = 25_000 * 16
    turns = [turn(24_900, 64_900), turn(0, 20_000), turn(20_000, 24_000)]
    chosen = select_fingerprint_turns(turns, audio_samples)
    assert [t.start_ms for t in chosen] == [0, 20_000]


def test_a_turn_running_past_the_end_of_the_audio_counts_only_what_exists():
    three_hundred_ms = 300 * 16
    assert select_fingerprint_turns([turn(0, 2000)], three_hundred_ms) == []


def test_no_usable_turns_gives_no_chunks():
    assert select_fingerprint_turns([], TEN_MINUTES) == []
