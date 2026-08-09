import inspect
from pathlib import Path

import pytest
from textual.widgets import Label, ListView, Markdown, Static

from voice_to_note.domain import Segment
from voice_to_note.tui import app as tui_app
from voice_to_note.tui.app import MemoApp

NOTES = {
    "title": "Sprint sync",
    "summary": "We agreed to ship on Friday.",
    "action_items": [{"task": "Cut the release", "owner": "Alice", "deadline": "Friday"}],
    "decisions": ["Ship on Friday"],
    "key_insights": [],
    "open_questions": [],
    "dates": [],
    "tags": ["release"],
}


def seed(repo) -> tuple[int, int]:
    """Two projects, one memo each, only the work memo having notes."""
    work = repo.create_memo(
        filename="standup.m4a", wav_path="/tmp/a.wav", duration_s=1.0, language="en",
        segments=[Segment(0, 1000, "we ship on friday", speaker="S1")], project="work",
    )
    repo.save_extraction(work, "claude", NOTES)
    home = repo.create_memo(
        filename="shopping.m4a", wav_path="/tmp/b.wav", duration_s=1.0, language="en",
        segments=[Segment(0, 1000, "buy milk", speaker="S1")], project="personal",
    )
    return work, home


def labels(view: ListView) -> list[str]:
    """What a person actually reads down the list, not the ids behind it."""
    return [str(item.query_one(Label).content) for item in view.children]


@pytest.mark.asyncio
async def test_the_app_opens_on_the_projects_that_exist(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        assert labels(pilot.app.query_one("#projects", ListView)) == [
            "personal (1)",
            "work (1)",
        ]


@pytest.mark.asyncio
async def test_choosing_a_project_narrows_the_memo_list(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()

        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]


@pytest.mark.asyncio
async def test_choosing_a_memo_shows_its_notes_and_its_transcript(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()

        assert "Sprint sync" in pilot.app.query_one("#notes", Markdown).source
        assert "we ship on friday" in str(pilot.app.query_one("#transcript", Static).content)


@pytest.mark.asyncio
async def test_a_memo_nobody_extracted_says_so_instead_of_failing(repo):
    _work, home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(home)
        await pilot.pause()

        assert "no notes" in pilot.app.query_one("#notes", Markdown).source.lower()
        assert "buy milk" in str(pilot.app.query_one("#transcript", Static).content)


@pytest.mark.asyncio
async def test_the_first_project_opens_without_a_keypress_to_wake_it(repo):
    # a sidebar highlighting nothing makes the first Enter of every session do
    # nothing at all, which reads as the app being broken
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("enter")

        assert labels(pilot.app.query_one("#memos", ListView)) == ["shopping.m4a"]


@pytest.mark.asyncio
async def test_the_keyboard_walks_on_to_the_next_project(repo):
    # the sidebar is focused on open, so every project is reachable without a mouse
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("down", "enter")

        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]


def test_the_tui_leaves_database_reads_and_formatting_to_services():
    # same rule as cli.py: the screen may hold a Repository, never query one
    source = Path(inspect.getsourcefile(tui_app)).read_text()

    assert "repo." not in source
    assert "from ..transforms" not in source
