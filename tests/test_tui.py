import inspect
from pathlib import Path

import pytest
from textual.widgets import Label, ListView, Markdown, Static, TextArea

from voice_to_note import services
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


def showing(app, selector: str) -> bool:
    """Whether the screen in front of the user has this on it. App.query looks at
    the default screen only, so a pushed dialog is invisible to it."""
    return bool(app.screen.query(selector))


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


# --- editing a note ------------------------------------------------------


@pytest.mark.asyncio
async def test_pressing_e_opens_the_note_for_editing(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        assert "Sprint sync" in pilot.app.screen.query_one("#editor", TextArea).text


@pytest.mark.asyncio
async def test_pressing_e_before_choosing_a_memo_does_nothing(repo):
    # there is nothing to edit yet, and offering an editor would save it nowhere
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()

        assert not showing(pilot.app, "#editor")


@pytest.mark.asyncio
async def test_saving_an_edit_stores_it_and_shows_it(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#editor", TextArea).text = "# In my own words"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert services.notes_markdown(repo, work) == "# In my own words"
        assert "In my own words" in pilot.app.query_one("#notes", Markdown).source
        assert not showing(pilot.app, "#editor")


@pytest.mark.asyncio
async def test_leaving_an_untouched_editor_closes_it_at_once(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert not showing(pilot.app, "#editor")
        assert not showing(pilot.app, "#confirm-discard")


@pytest.mark.asyncio
async def test_leaving_an_edited_note_asks_before_throwing_it_away(repo):
    # a stray escape must not silently bin what somebody just wrote
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#editor", TextArea).text = "# Half-written"
        await pilot.press("escape")
        await pilot.pause()

        assert showing(pilot.app, "#confirm-discard")
        assert services.notes_markdown(repo, work).startswith("# Sprint sync")


@pytest.mark.asyncio
async def test_saving_an_emptied_note_says_so_and_keeps_what_was_typed(repo):
    # a note cannot be blank, but finding that out must not cost the editor
    # session or take the app down with it
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#editor", TextArea).text = "   \n  "
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert showing(pilot.app, "#editor")
        assert [str(n.message) for n in pilot.app._notifications] == ["a note needs something in it"]
        assert services.notes_markdown(repo, work).startswith("# Sprint sync")


@pytest.mark.asyncio
async def test_agreeing_to_discard_closes_everything_and_keeps_the_stored_note(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#editor", TextArea).text = "# Half-written"
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("y")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-discard")
        assert not showing(pilot.app, "#editor")
        assert services.notes_markdown(repo, work).startswith("# Sprint sync")


@pytest.mark.asyncio
async def test_declining_to_discard_puts_them_back_in_their_own_writing(repo):
    # the whole point of asking: saying no has to return the text, not a blank
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#editor", TextArea).text = "# Half-written"
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-discard")
        assert pilot.app.screen.query_one("#editor", TextArea).text == "# Half-written"
