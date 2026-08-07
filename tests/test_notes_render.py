from voice_to_note.domain import Extraction
from voice_to_note.transforms.notes import render_notes

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


def render(**overrides) -> str:
    return render_notes(Extraction("claude", {**BLANK, **overrides}, "2026-08-07 12:00:00"))


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


def test_list_sections_render_as_bullets():
    out = render(decisions=["cut scope"], key_insights=["onboarding is slow"])
    assert "DECISIONS\n  - cut scope\n" in out
    assert "KEY INSIGHTS\n  - onboarding is slow\n" in out


def test_dates_pair_the_date_with_its_context():
    out = render(dates=[{"date": "Monday", "context": "kickoff"}])
    assert "DATES\n  - Monday: kickoff\n" in out


def test_tags_render_as_a_single_trailing_line():
    assert render(tags=["release", "hiring"]).endswith("tags: release, hiring")
