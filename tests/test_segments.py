from voice_to_note.transforms.segments import segments_from_whisper

RAW = {
    "transcription": [
        {"text": " Hello there. ", "offsets": {"from": 0, "to": 1500}},
        {"text": "   ", "offsets": {"from": 1500, "to": 1600}},
        {"text": "[BLANK_AUDIO]", "offsets": {"from": 1600, "to": 2000}},
        {"text": "Second one.", "offsets": {"from": 2000, "to": 3000}},
    ]
}


def test_offsets_map_to_millisecond_bounds():
    first = segments_from_whisper(RAW)[0]
    assert (first.t0_ms, first.t1_ms) == (0, 1500)


def test_text_is_stripped():
    assert segments_from_whisper(RAW)[0].text == "Hello there."


def test_empty_text_segments_are_dropped():
    assert [s.text for s in segments_from_whisper(RAW)] == [
        "Hello there.",
        "[BLANK_AUDIO]",
        "Second one.",
    ]


def test_missing_transcription_key_gives_no_segments():
    assert segments_from_whisper({}) == []
