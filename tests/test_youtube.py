import json

import pytest

from voice_to_note.domain import Segment
from voice_to_note.transforms.youtube import (
    MAX_PARAGRAPH_MS,
    caption_langs,
    pick_track,
    segments_from_captions,
    video_from_info,
)


def info(**overrides):
    base = {
        "id": "abc123",
        "title": "How to think",
        "channel": "Some Channel",
        "duration": 3852,
        "upload_date": "20260115",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "language": "en",
        "is_live": False,
        "subtitles": {},
        "automatic_captions": {},
    }
    base.update(overrides)
    return base


def video(**overrides):
    return video_from_info(info(**overrides))


def track(url="https://captions.example/t", ext="json3"):
    return {"url": url, "ext": ext}


def ev(t0_ms, dur_ms, text):
    return {"tStartMs": t0_ms, "dDurationMs": dur_ms, "segs": [{"utf8": text}]}


def json3(*events):
    return json.dumps({"events": list(events)})


# --- reading the info dump --------------------------------------------------


def test_the_canonical_watch_url_is_what_the_video_is_known_by():
    assert video().url == "https://www.youtube.com/watch?v=abc123"


def test_the_upload_day_becomes_a_recorded_at_stamp_at_midnight_utc():
    assert video().uploaded_at == "2026-01-15T00:00:00Z"


def test_a_video_that_never_said_its_upload_day_carries_no_stamp():
    assert video(upload_date=None).uploaded_at is None


def test_the_uploader_names_the_channel_when_no_channel_is_given():
    assert video(channel=None, uploader="someone").channel == "someone"


def test_an_info_dump_missing_its_title_names_the_missing_field():
    bad = info()
    del bad["title"]

    with pytest.raises(ValueError, match="no 'title'"):
        video_from_info(bad)


# --- choosing a caption track -----------------------------------------------


def test_manual_subtitles_win_over_auto_captions_in_the_same_language():
    v = video(
        subtitles={"en": [track("https://captions.example/manual")]},
        automatic_captions={"en": [track("https://captions.example/auto")]},
    )

    assert pick_track(v, ["en"]) == ("en", "https://captions.example/manual", False)


def test_manual_subtitles_in_any_language_win_over_auto_in_the_preferred_one():
    v = video(
        subtitles={"fr": [track("https://captions.example/fr")]},
        automatic_captions={"en": [track("https://captions.example/en")]},
    )

    assert pick_track(v, ["en"]) == ("fr", "https://captions.example/fr", False)


def test_the_preferred_language_list_is_honoured_in_order():
    v = video(
        automatic_captions={
            "de": [track("https://captions.example/de")],
            "es": [track("https://captions.example/es")],
        }
    )

    assert pick_track(v, ["es", "de"]) == ("es", "https://captions.example/es", True)


def test_a_bare_language_finds_its_regional_variant():
    v = video(automatic_captions={"en-US": [track("https://captions.example/us")]})

    assert pick_track(v, ["en"]) == ("en-US", "https://captions.example/us", True)


def test_an_unmatched_preference_falls_back_to_english_then_anything():
    v = video(language=None, automatic_captions={"ja": [track("https://captions.example/ja")]})

    assert pick_track(v, ["fr"]) == ("ja", "https://captions.example/ja", True)


def test_a_strict_ask_returns_nothing_instead_of_a_surprise_language():
    v = video(automatic_captions={"ja": [track()]})

    assert pick_track(v, ["fr"], any_language=False) is None


def test_a_video_with_no_captions_picks_no_track():
    assert pick_track(video(), ["en"]) is None


def test_the_json3_format_is_taken_when_a_language_offers_several():
    v = video(
        subtitles={
            "en": [track("https://captions.example/vtt", ext="vtt"), track("https://captions.example/j3")]
        }
    )

    assert pick_track(v, ["en"]) == ("en", "https://captions.example/j3", False)


def test_caption_langs_names_every_language_across_both_kinds():
    v = video(subtitles={"fr": [track()]}, automatic_captions={"en": [track()], "de": [track()]})

    assert caption_langs(v) == ["de", "en", "fr"]


# --- folding caption events into paragraphs ---------------------------------


def test_a_short_run_of_events_becomes_one_timed_paragraph():
    text = json3(ev(0, 2000, "hello there"), ev(2000, 2000, "and welcome"))

    assert segments_from_captions(text) == [Segment(0, 4000, "hello there and welcome")]


def test_caption_events_group_into_paragraphs_cut_at_pauses():
    # half a minute of talk, a three-second breath, then more: the cut lands
    # in the pause rather than mid-sentence
    talking = [ev(i * 5000, 5000, f"part {i}") for i in range(7)]  # 0s..35s
    text = json3(*talking, ev(38000, 2000, "after the pause"))

    paragraphs = segments_from_captions(text)

    assert [p.t0_ms for p in paragraphs] == [0, 38000]
    assert paragraphs[0].t1_ms == 35000
    assert paragraphs[1].text == "after the pause"


def test_an_unbroken_stream_of_speech_is_cut_at_the_length_cap():
    text = json3(*[ev(i * 5000, 5000, f"part {i}") for i in range(20)])  # 0s..100s

    paragraphs = segments_from_captions(text)

    assert len(paragraphs) == 2
    assert paragraphs[0].t1_ms <= MAX_PARAGRAPH_MS
    assert paragraphs[1].t0_ms == paragraphs[0].t1_ms


def test_newline_only_events_never_reach_a_segment():
    text = json3(ev(0, 1000, "\n"), ev(1000, 1000, "real words"), {"tStartMs": 3000})

    assert [p.text for p in segments_from_captions(text)] == ["real words"]


def test_a_track_that_is_not_json_names_the_format_problem():
    with pytest.raises(ValueError, match="not json3"):
        segments_from_captions("WEBVTT\n\n00:00.000 --> 00:02.000\nhello")


# --- describing a video's captions --------------------------------------------


def translated(lang):
    """How yt-dlp lists YouTube's machine translation into a language: an
    automatic_captions entry whose URL asks for it with tlang."""
    return [{"url": f"https://captions.example/api?lang=en&tlang={lang}", "ext": "json3",
             "name": lang}]


def described(v, preferred, **kw):
    from voice_to_note.transforms.youtube import describe_captions

    return [(c.code, c.name, c.kind, c.taken) for c in describe_captions(v, preferred, **kw)]


def test_a_video_with_only_written_captions_lists_them_by_name():
    v = video(subtitles={"en": [track()], "fr": [track()]})

    assert described(v, ["en"]) == [
        ("en", "English", "manual", True),
        ("fr", "French", "manual", False),
    ]


def test_a_video_with_only_auto_captions_marks_them_as_auto_generated():
    v = video(automatic_captions={"en-orig": [track()], "en": translated("en")})

    assert described(v, ["en"]) == [("en", "English", "auto", True)]


def test_a_language_both_written_and_recognised_is_listed_once_as_written():
    v = video(subtitles={"en": [track()]}, automatic_captions={"en-orig": [track()]})

    assert described(v, ["en"]) == [("en", "English", "manual", True)]


def test_machine_translations_are_listed_only_into_a_preferred_language():
    v = video(
        automatic_captions={
            "en-orig": [track()], "pl": translated("pl"), "de": translated("de"),
        }
    )

    assert described(v, ["pl", "en"]) == [
        ("pl", "Polish", "translated", False),
        ("en", "English", "auto", True),
    ]


def test_a_translation_is_never_taken_over_a_track_the_video_really_has():
    v = video(automatic_captions={"en-orig": [track("https://c/en")], "pl": translated("pl")})

    assert pick_track(v, ["pl"])[0] == "en"


def test_a_translation_is_taken_when_the_language_was_asked_for_by_name():
    v = video(automatic_captions={"en-orig": [track("https://c/en")], "pl": translated("pl")})

    picked = pick_track(v, ["pl"], any_language=False)

    assert picked is not None and picked[0] == "pl" and picked[2] is True


def test_a_translation_only_video_is_taken_in_the_preferred_language():
    v = video(language=None, automatic_captions={"pl": translated("pl"), "de": translated("de")})

    assert pick_track(v, ["pl"])[0] == "pl"
    assert pick_track(v, ["fr"]) is None


def test_the_chooser_walks_the_preference_list_in_order():
    v = video(subtitles={"pl": [track("https://c/pl")], "uk": [track("https://c/uk")]})

    assert pick_track(v, ["uk", "pl"])[0] == "uk"
    assert pick_track(v, ["pl", "uk"])[0] == "pl"


def test_preferred_languages_come_first_in_the_order_asked_and_the_taken_one_is_marked():
    v = video(
        language="de",
        subtitles={"de": [track()], "uk": [track()]},
        automatic_captions={"pl": [track()], "de-orig": [track()]},
    )

    assert described(v, ["uk", "pl"]) == [
        ("uk", "Ukrainian", "manual", True),
        ("pl", "Polish", "auto", False),
        ("de", "German", "manual", False),
    ]


def test_a_video_with_no_captions_describes_nothing():
    assert described(video(), ["en"]) == []


def test_a_language_matches_its_regional_track_without_regard_to_case():
    v = video(subtitles={"pt-BR": [track()]})

    assert pick_track(v, ["pt-br"], any_language=False)[0] == "pt-BR"


def test_the_label_names_the_kind_unless_a_person_wrote_it():
    from voice_to_note.transforms.youtube import CaptionLang, caption_label

    assert [
        caption_label(CaptionLang("en", "English", "manual")),
        caption_label(CaptionLang("uk", "Ukrainian", "auto")),
        caption_label(CaptionLang("pl", "Polish", "translated")),
    ] == ["English", "Ukrainian (auto-generated)", "Polish (translated)"]
