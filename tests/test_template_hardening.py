import argparse
import json
import sys

import pytest
from conftest import StubRepo

from voice_to_note import cli, config, services
from voice_to_note.transforms.notes import parse_notes
from voice_to_note.transforms.templates import MAX_SECTIONS, parse_template

NOTES = {
    "title": "Sprint sync",
    "summary": "We discussed the release.",
    "action_items": [],
    "decisions": [],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": [],
}


def run(monkeypatch, *argv) -> None:
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: StubRepo())
    monkeypatch.setattr(sys, "argv", ["vtn", *argv])
    cli.main()


@pytest.fixture
def templates_dir(monkeypatch, tmp_path):
    path = tmp_path / "templates"
    path.mkdir()
    monkeypatch.setattr(config, "TEMPLATES_DIR", path)
    return path


# --- an unreadable template file ------------------------------------------


def test_a_template_file_that_is_not_utf8_is_refused_naming_the_file(templates_dir):
    (templates_dir / "binary.md").write_bytes(b"\xff\xfe\x00 not text")

    with pytest.raises(services.InvalidInput, match=r"binary\.md.*cannot be read"):
        services.note_template("binary")
    with pytest.raises(services.InvalidInput, match=r"binary\.md"):
        services.template_text("binary")
    with pytest.raises(services.InvalidInput, match=r"binary\.md"):
        services.template_sections("binary")


def test_the_template_listing_shows_an_unreadable_file_instead_of_crashing(
    templates_dir, monkeypatch, capsys
):
    (templates_dir / "binary.md").write_bytes(b"\xff\xfe\x00 not text")

    run(monkeypatch, "template")

    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("binary"))
    assert "unreadable" in line


# --- front matter robustness -------------------------------------------------


def test_a_byte_order_mark_before_the_fence_does_not_disable_front_matter():
    parsed = parse_template('﻿+++\n[sections]\n"A" = "b"\n+++\nbody')

    assert parsed.sections == (("A", "b"),)
    assert parsed.body == "body"


def test_blank_lines_before_the_fence_do_not_disable_front_matter():
    parsed = parse_template('\n\n  \n+++\n[sections]\n"A" = "b"\n+++\nbody')

    assert parsed.sections == (("A", "b"),)


def test_front_matter_with_windows_line_endings_is_read_and_stripped():
    parsed = parse_template('+++\r\n[sections]\r\n"A" = "b"\r\n+++\r\nbody\r\n')

    assert parsed.sections == (("A", "b"),)
    assert parsed.body == "body\r\n"
    assert "+++" not in parsed.body


# --- the template is only needed when the notes are --------------------------


def test_a_stale_default_does_not_stop_a_run_whose_steps_skip_the_notes(monkeypatch):
    monkeypatch.setattr(config, "RECORD_TEMPLATE", "gone")
    args = argparse.Namespace(template=None)

    cli._settled_template(args, "record", frozenset({"speakers"}))

    assert args.template is None


def test_a_stale_default_still_stops_a_run_whose_steps_include_the_notes(monkeypatch):
    monkeypatch.setattr(config, "RECORD_TEMPLATE", "gone")

    with pytest.raises(services.InvalidInput, match="record_template"):
        cli._settled_template(argparse.Namespace(template=None), "record", frozenset({"notes"}))


def test_a_bad_template_flag_is_refused_even_when_the_notes_step_is_skipped(monkeypatch):
    with pytest.raises(services.InvalidInput, match="unknown note template"):
        cli._settled_template(argparse.Namespace(template="bogus"), "file", frozenset())


def test_youtube_without_the_notes_step_ignores_a_stale_default(monkeypatch, capsys):
    monkeypatch.setattr(config, "YOUTUBE_TEMPLATE", "gone")
    reached = []

    def video(_url):
        reached.append(True)
        raise services.InvalidInput("stop here")

    monkeypatch.setattr(services, "youtube_video", video)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, "youtube", "https://youtu.be/abc123", "--steps", "")

    assert reached == [True]
    assert "youtube_template" not in str(err.value.code)


# --- section headings and counts ---------------------------------------------


def reply(sections) -> str:
    return json.dumps({**NOTES, "sections": sections})


def test_a_heading_with_a_newline_is_made_one_line_without_leading_marks():
    data = parse_notes(reply([{"heading": "## Why\n   it matters ", "body": "text\n\nmore"}]))

    assert data["sections"] == [{"heading": "Why it matters", "body": "text\n\nmore"}]


def test_a_section_whose_heading_is_only_marks_is_dropped():
    data = parse_notes(reply([{"heading": " ### \n", "body": "text"}]))

    assert "sections" not in data


def test_the_number_of_sections_kept_is_capped():
    many = [{"heading": f"H{i}", "body": "b"} for i in range(MAX_SECTIONS + 5)]

    assert len(parse_notes(reply(many))["sections"]) == MAX_SECTIONS


def test_a_template_heading_is_normalised_like_a_reply_heading():
    parsed = parse_template('+++\n[sections]\n"## Big\\nidea" = "b"\n+++\nbody')

    assert parsed.sections == (("Big idea", "b"),)
