import json

import pytest

from voice_to_note import config
from voice_to_note.domain import Extraction
from voice_to_note.gateways import llm
from voice_to_note.transforms.notes import (
    parse_notes,
    render_notes,
    render_notes_markdown,
)
from voice_to_note.transforms.templates import (
    ParsedTemplate,
    TemplateError,
    compose_prompt,
    parse_template,
)

NOTES = {
    "title": "Sprint sync",
    "summary": "We discussed the release.",
    "action_items": [],
    "decisions": [],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": ["video"],
}

WITH_FRONT_MATTER = """+++
[sections]
"Worth watching?" = "Yes or no, and why."
"Questions answered" = "Each question, with its answer."
+++
Extract notes.

Transcript:
"""


def reply(**extra) -> str:
    return json.dumps({**NOTES, **extra})


# --- front matter -------------------------------------------------------


def test_front_matter_declares_sections_in_file_order_and_is_stripped_from_the_body():
    parsed = parse_template(WITH_FRONT_MATTER)

    assert parsed.sections == (
        ("Worth watching?", "Yes or no, and why."),
        ("Questions answered", "Each question, with its answer."),
    )
    assert parsed.body == "Extract notes.\n\nTranscript:\n"
    assert "+++" not in parsed.body


def test_a_template_without_front_matter_is_returned_whole():
    text = "Extract notes.\n\n+++ not a fence here\nTranscript:\n"

    assert parse_template(text) == ParsedTemplate(body=text)


def test_a_fence_that_is_not_on_the_first_line_is_just_text():
    text = "Intro\n+++\n[sections]\n+++\nTranscript:\n"

    assert parse_template(text).sections == ()


def test_unknown_tables_are_kept_for_a_later_reader_rather_than_rejected():
    parsed = parse_template(
        '+++\n[ask]\nwhy = "Why does it matter?"\n[sections]\n"A" = "b"\n+++\nbody'
    )

    assert parsed.tables == {"ask": {"why": "Why does it matter?"}}
    assert parsed.sections == (("A", "b"),)


def test_front_matter_that_is_not_toml_is_refused():
    with pytest.raises(TemplateError, match="not valid TOML"):
        parse_template("+++\n[sections\n+++\nbody")


def test_front_matter_that_is_never_closed_is_refused():
    with pytest.raises(TemplateError, match="never closed"):
        parse_template('+++\n[sections]\n"A" = "b"\nbody')


def test_a_section_whose_text_is_not_a_string_is_refused():
    with pytest.raises(TemplateError, match="needs a heading"):
        parse_template('+++\n[sections]\n"A" = 3\n+++\nbody')


def test_sections_that_are_not_a_table_are_refused():
    with pytest.raises(TemplateError, match="must be a table"):
        parse_template('+++\nsections = "x"\n+++\nbody')


# --- the prompt -----------------------------------------------------------


def test_the_prompt_asks_for_the_sections_and_still_ends_on_the_transcript():
    prompt = compose_prompt(parse_template(WITH_FRONT_MATTER), "THE WORDS SAID")

    assert prompt.endswith("Transcript:\nTHE WORDS SAID")
    assert '"sections"' in prompt
    assert '- "Worth watching?": Yes or no, and why.' in prompt
    assert prompt.index('"Worth watching?"') < prompt.index("Transcript:")
    assert prompt.index('"Worth watching?"') < prompt.index('"Questions answered"')


def test_a_template_not_ending_on_a_transcript_line_gets_the_instruction_appended():
    prompt = compose_prompt(
        parse_template('+++\n[sections]\n"A" = "b"\n+++\nDo the thing.'), "TEXT"
    )

    assert prompt.startswith("Do the thing.")
    assert '- "A": b' in prompt
    assert prompt.endswith("TEXT")


def test_a_template_with_no_sections_is_sent_exactly_as_before():
    assert compose_prompt(parse_template("Body\nTranscript:\n"), "T") == "Body\nTranscript:\nT"


def test_a_broken_custom_template_is_reported_with_the_file_it_is_in(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path)
    (tmp_path / "bad.md").write_text("+++\n[sections\n+++\nbody")

    with pytest.raises(TemplateError, match=r"bad\.md"):
        llm.parsed_template("bad")


def test_the_video_built_in_declares_its_sections_through_front_matter(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path)

    parsed = llm.parsed_template("video")

    assert [heading for heading, _ in parsed.sections] == [
        "Worth watching?",
        "Questions answered",
        "Hidden insights",
    ]
    assert "video" in llm.NOTE_TEMPLATES
    assert llm.notes_prompt("SAID", "video").endswith("Transcript:\nSAID")


# --- parsing the reply ----------------------------------------------------


def test_a_reply_without_sections_parses_as_before():
    assert "sections" not in parse_notes(reply())


def test_well_formed_sections_are_kept_in_order():
    data = parse_notes(
        reply(sections=[{"heading": "A", "body": "one"}, {"heading": "B", "body": "two"}])
    )

    assert data["sections"] == [{"heading": "A", "body": "one"}, {"heading": "B", "body": "two"}]


def test_malformed_section_entries_are_dropped_without_failing_the_extraction():
    data = parse_notes(
        reply(
            sections=[
                {"heading": "Kept", "body": "yes"},
                {"heading": "No body"},
                {"heading": 3, "body": "x"},
                "just a string",
                {"heading": "  ", "body": "blank heading"},
            ]
        )
    )

    assert data["sections"] == [{"heading": "Kept", "body": "yes"}]


def test_sections_of_the_wrong_type_are_dropped_and_the_notes_survive():
    data = parse_notes(reply(sections="not a list"))

    assert data["title"] == "Sprint sync"
    assert "sections" not in data


# --- rendering --------------------------------------------------------------


def extraction(**overrides) -> Extraction:
    data = {**NOTES, **overrides, "dates": [{"date": "May 1", "context": "launch"}]}
    return Extraction("claude", data, "2026-08-07 12:00:00")


SECTIONS = [{"heading": "Worth watching?", "body": "Yes.\n\nIt is short."}]


def test_the_plain_notes_show_sections_after_the_fixed_ones_and_before_the_tags():
    out = render_notes(extraction(sections=SECTIONS))

    assert out.index("DATES") < out.index("WORTH WATCHING?") < out.index("tags:")
    assert "  Yes.\n\n  It is short." in out


def test_the_markdown_notes_show_sections_after_the_fixed_ones_and_before_the_tags():
    out = render_notes_markdown(extraction(sections=SECTIONS))

    assert out.index("## Dates") < out.index("## Worth watching?") < out.index("Tags:")
    assert "## Worth watching?\n\nYes.\n\nIt is short." in out


def test_notes_say_which_template_made_them_only_when_it_is_known():
    known = Extraction("claude", NOTES, "2026-08-07 12:00:00", "video")

    assert "template video" in render_notes(known)
    assert "template video" in render_notes_markdown(known)
    assert "template" not in render_notes(extraction())
