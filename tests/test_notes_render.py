from markdown_it import MarkdownIt

from voice_to_note.domain import Extraction
from voice_to_note.transforms.notes import render_notes, render_notes_markdown

BLANK = {
    "title": "Sprint sync",
    "summary": "We discussed the release.",
    "action_items": [],
    "decisions": [],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": [],
}


def render(done=None, **overrides) -> str:
    return render_notes(
        Extraction("claude", {**BLANK, **overrides}, "2026-08-07 12:00:00"), done
    )


def test_header_carries_title_backend_and_time():
    assert render().startswith(
        "# Sprint sync\n  (claude, 2026-08-07 12:00:00)\n\nWe discussed the release."
    )


def test_empty_sections_are_left_out():
    out = render()
    for header in ("ACTION ITEMS", "DECISIONS", "KEY INSIGHTS", "OPEN QUESTIONS", "DATES", "tags:"):
        assert header not in out


def test_action_item_shows_owner_and_deadline():
    out = render(action_items=[{"task": "ship it", "owner": "Alice", "deadline": "Friday"}])
    assert "  [ ] ship it  (Alice, Friday)" in out.splitlines()


def test_action_item_with_only_an_owner():
    out = render(action_items=[{"task": "ship it", "owner": "Alice", "deadline": None}])
    assert "  [ ] ship it  (Alice)" in out.splitlines()


def test_action_item_with_only_a_deadline():
    out = render(action_items=[{"task": "ship it", "owner": None, "deadline": "Friday"}])
    assert "  [ ] ship it  (Friday)" in out.splitlines()


def test_action_item_with_neither_has_no_parentheses():
    out = render(action_items=[{"task": "ship it", "owner": None, "deadline": None}])
    assert "  [ ] ship it" in out.splitlines()


def test_a_task_checked_off_is_the_only_one_whose_box_is_ticked():
    out = render(
        done={"ship it"},
        action_items=[
            {"task": "ship it", "owner": "Alice", "deadline": "Friday"},
            {"task": "book the room", "owner": None, "deadline": None},
        ],
    )
    assert "  [x] ship it  (Alice, Friday)" in out.splitlines()
    assert "  [ ] book the room" in out.splitlines()


def test_a_task_reworded_since_it_was_checked_off_still_reads_as_done():
    # notes are regenerated whole, so a commitment comes back worded however the
    # model put it that time; a box that untucked itself over a full stop would
    # ask for work somebody has already done
    reworded = {"task": "Cut  The Release.", "owner": None, "deadline": None}
    markdown = render_notes_markdown(
        Extraction(
            "claude", {**BLANK, "action_items": [reworded]}, "2026-08-07 12:00:00"
        ),
        {"cut the release"},
    )

    assert "- [x] Cut  The Release." in markdown.splitlines()


def test_list_sections_render_as_bullets():
    out = render(decisions=["cut scope"], key_insights=["onboarding is slow"])
    assert "DECISIONS\n  - cut scope\n" in out
    assert "KEY INSIGHTS\n  - onboarding is slow\n" in out


def test_dates_pair_the_date_with_its_context():
    out = render(dates=[{"date": "Monday", "context": "kickoff"}])
    assert "DATES\n  - Monday: kickoff\n" in out


def test_tags_render_as_a_single_trailing_line():
    assert render(tags=["release", "hiring"]).endswith("tags: release, hiring")


def test_a_task_written_in_markdown_stays_one_item():
    # models write asterisks and brackets into task text; whatever that renders
    # as, it must not break one commitment into several list items or headings
    awkward = {"task": "fix *urgent* [#123] bug", "owner": None, "deadline": None}
    markdown = render_notes_markdown(
        Extraction("claude", {**BLANK, "action_items": [awkward]}, "2026-08-07 12:00:00")
    )

    items = [line for line in markdown.splitlines() if line.startswith("- [ ]")]
    assert items == ["- [ ] fix *urgent* [#123] bug"]
    # parsed by the same library the Markdown widget uses: one list, one item
    blocks = [t.type for t in MarkdownIt("gfm-like").parse(markdown)]
    assert blocks.count("bullet_list_open") == 1
    assert blocks.count("list_item_open") == 1
