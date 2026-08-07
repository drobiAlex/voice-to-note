import json

import pytest

from voice_to_note.transforms.notes import parse_notes

NOTES = {
    "title": "Sprint sync",
    "summary": "We discussed the release.",
    "action_items": [{"task": "ship it", "owner": "Alice", "deadline": "Friday"}],
    "decisions": ["cut scope"],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": ["release"],
}


def test_parses_clean_json():
    assert parse_notes(json.dumps(NOTES)) == NOTES


def test_strips_markdown_fences():
    assert parse_notes(f"```json\n{json.dumps(NOTES)}\n```") == NOTES


def test_strips_think_block():
    text = f"<think>the user wants {{notes}}</think>\n{json.dumps(NOTES)}"
    assert parse_notes(text) == NOTES


def test_extracts_json_embedded_in_prose():
    text = f"Sure! Here are the notes:\n{json.dumps(NOTES)}\nLet me know if you need more."
    assert parse_notes(text) == NOTES


def test_rejects_output_missing_required_keys():
    incomplete = {k: v for k, v in NOTES.items() if k != "tags"}
    with pytest.raises(ValueError, match="tags"):
        parse_notes(json.dumps(incomplete))


def test_rejects_output_without_json():
    with pytest.raises(ValueError):
        parse_notes("I could not find anything to extract.")
