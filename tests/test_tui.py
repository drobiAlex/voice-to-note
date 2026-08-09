import inspect
import threading
from pathlib import Path

import pytest
from textual.widgets import (
    Input,
    Label,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TextArea,
)

from voice_to_note import services
from voice_to_note.domain import Segment, Speaker
from voice_to_note.gateways import GatewayError
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
        segments=[Segment(0, 1000, "we ship on friday", speaker="S1")],
        speakers=[Speaker("S1")], project="work",
    )
    repo.save_extraction(work, "claude", NOTES)
    home = repo.create_memo(
        filename="shopping.m4a", wav_path="/tmp/b.wav", duration_s=1.0, language="en",
        segments=[Segment(0, 1000, "buy milk", speaker="S1")],
        speakers=[Speaker("S1")], project="personal",
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


# --- refiling and renaming from the screen --------------------------------


async def open_memo(pilot, memo_id: int) -> None:
    """Puts a memo on screen, the state both of these modals need."""
    pilot.app.show_memo(memo_id)
    await pilot.pause()


@pytest.mark.asyncio
async def test_pressing_m_offers_the_projects_a_memo_could_join(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()

        assert labels(pilot.app.screen.query_one("#project-choices", ListView)) == [
            "personal",
            "work",
        ]


@pytest.mark.asyncio
async def test_typing_a_new_project_refiles_the_memo_and_moves_the_count(repo):
    # projects are free text, so the screen has to accept one nobody used yet
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "side"
        await pilot.press("enter")
        await pilot.pause()

        assert not showing(pilot.app, "#project-name")
        assert [m.project for m in services.memos(repo) if m.id == work] == ["side"]
        assert labels(pilot.app.query_one("#projects", ListView)) == ["personal (1)", "side (1)"]


@pytest.mark.asyncio
async def test_choosing_an_existing_project_from_the_list_refiles_the_memo(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-choices", ListView).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert [m.project for m in services.memos(repo) if m.id == work] == ["personal"]


@pytest.mark.asyncio
async def test_a_project_name_of_nothing_is_refused_without_losing_the_modal(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert showing(pilot.app, "#project-name")
        assert [str(n.message) for n in pilot.app._notifications] == ["a project needs a name"]
        assert [m.project for m in services.memos(repo) if m.id == work] == ["work"]


@pytest.mark.asyncio
async def test_leaving_the_move_alone_leaves_the_memo_where_it_was(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert not showing(pilot.app, "#project-name")
        assert [m.project for m in services.memos(repo) if m.id == work] == ["work"]


@pytest.mark.asyncio
async def test_pressing_m_before_choosing_a_memo_does_nothing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("m")
        await pilot.pause()

        assert not showing(pilot.app, "#project-name")


@pytest.mark.asyncio
async def test_pressing_r_offers_the_speakers_in_this_memo(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()

        assert labels(pilot.app.screen.query_one("#speaker-choices", ListView)) == ["S1 — S1"]


@pytest.mark.asyncio
async def test_naming_a_speaker_shows_that_name_in_the_transcript(repo):
    # the point of renaming: the transcript stops saying S1 at you
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()
        pilot.app.screen.query_one("#speaker-name", Input).value = "Samantha"
        await pilot.press("enter")
        await pilot.pause()

        assert not showing(pilot.app, "#speaker-name")
        assert "Samantha: we ship on friday" in str(
            pilot.app.query_one("#transcript", Static).content
        )


@pytest.mark.asyncio
async def test_leaving_the_rename_alone_leaves_the_speaker_named_as_it_was(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert not showing(pilot.app, "#speaker-name")
        assert services.speakers(repo, work) == {"S1": "S1"}


@pytest.mark.asyncio
async def test_pressing_r_before_choosing_a_memo_does_nothing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()

        assert not showing(pilot.app, "#speaker-choices")


@pytest.mark.asyncio
async def test_a_speaker_name_of_nothing_is_refused_without_losing_the_modal(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()
        pilot.app.screen.query_one("#speaker-name", Input).value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert showing(pilot.app, "#speaker-name")
        assert [str(n.message) for n in pilot.app._notifications] == ["a speaker needs a name"]
        assert services.speakers(repo, work) == {"S1": "S1"}


@pytest.mark.asyncio
async def test_choosing_a_speaker_from_the_list_stays_inside_the_modal(repo):
    # the app behind reads a list selection as a memo id; "S1" is not one
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()
        pilot.app.screen.query_one("#speaker-choices", ListView).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert showing(pilot.app, "#speaker-name")


@pytest.mark.asyncio
async def test_choosing_a_speaker_moves_on_to_typing_the_name(repo):
    # picking the voice is only half of it: the name still has to be typed
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("r")
        await pilot.pause()
        pilot.app.screen.query_one("#speaker-choices", ListView).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen.focused is pilot.app.screen.query_one("#speaker-name", Input)


@pytest.mark.asyncio
async def test_moving_the_shown_memo_out_of_view_stops_showing_it(repo):
    # its project is gone from the sidebar, its row is gone from the list, so
    # leaving the transcript up is showing something the screen says is not there
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "side"
        await pilot.press("enter")
        await pilot.pause()

        assert labels(pilot.app.query_one("#memos", ListView)) == []
        assert "we ship on friday" not in str(
            pilot.app.query_one("#transcript", Static).content
        )


# --- reading the transcript raw -------------------------------------------


def repair(repo, memo_id: int, text: str) -> None:
    """Puts a repair pass over a memo's one line: what the repaired view shows
    and the raw view is there to see behind."""
    (segment,) = repo.segments(memo_id)
    repo.update_refinements(memo_id, {segment.id: text})


def transcript(pilot) -> str:
    """The transcript as it currently reads on screen."""
    return str(pilot.app.query_one("#transcript", Static).content)


def transcript_tab(pilot) -> str:
    """What the tab strip calls the transcript, which is where the screen says
    which of the two versions is under it."""
    return str(pilot.app.query_one(TabbedContent).get_tab("transcript-tab").label)


@pytest.mark.asyncio
async def test_pressing_t_shows_the_words_as_they_were_actually_transcribed(repo):
    # a repair can drop or reword what was said; raw is how you check it
    work, _home = seed(repo)
    repair(repo, work, "We ship on Friday.")

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        assert "We ship on Friday." in transcript(pilot)

        await pilot.press("t")
        await pilot.pause()

        assert "we ship on friday" in transcript(pilot)
        assert "We ship on Friday." not in transcript(pilot)


@pytest.mark.asyncio
async def test_pressing_t_again_puts_the_repaired_transcript_back(repo):
    work, _home = seed(repo)
    repair(repo, work, "We ship on Friday.")

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()

        assert "We ship on Friday." in transcript(pilot)
        assert transcript_tab(pilot) == "Transcript"


@pytest.mark.asyncio
async def test_the_transcript_tab_says_which_version_is_under_it(repo):
    # the two versions can differ by one word, so the screen has to say which
    work, _home = seed(repo)
    repair(repo, work, "We ship on Friday.")

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        assert transcript_tab(pilot) == "Transcript"

        await pilot.press("t")
        await pilot.pause()

        assert transcript_tab(pilot) == "Transcript (raw)"


@pytest.mark.asyncio
async def test_reading_raw_holds_when_the_next_memo_is_opened(repo):
    # somebody checking the repairs is checking all of them, not re-pressing t
    work, home = seed(repo)
    repair(repo, work, "We ship on Friday.")
    repair(repo, home, "Buy milk.")

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("t")
        await pilot.pause()
        await open_memo(pilot, home)

        assert "buy milk" in transcript(pilot)
        assert "Buy milk." not in transcript(pilot)


@pytest.mark.asyncio
async def test_reading_raw_survives_the_memo_it_was_reading_going_away(repo):
    # the mode is how somebody is reading, not something the vanished memo owned
    work, home = seed(repo)
    repair(repo, work, "We ship on Friday.")
    repair(repo, home, "Buy milk.")

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "side"
        await pilot.press("enter")
        await pilot.pause()
        await open_memo(pilot, home)

        assert "buy milk" in transcript(pilot)
        assert transcript_tab(pilot) == "Transcript (raw)"


@pytest.mark.asyncio
async def test_a_memo_moved_out_of_view_takes_the_raw_claim_with_it(repo):
    # the panes go empty, so there is no longer a transcript under the tab for
    # the word raw to be describing
    work, _home = seed(repo)
    repair(repo, work, "We ship on Friday.")

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "side"
        await pilot.press("enter")
        await pilot.pause()

        assert transcript_tab(pilot) == "Transcript"


@pytest.mark.asyncio
async def test_pressing_t_before_choosing_a_memo_does_nothing(repo):
    # there is no transcript under the tab yet, so calling it raw would be a lie
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()

        assert transcript_tab(pilot) == "Transcript"


# --- renaming and emptying projects ---------------------------------------


async def point_at_project(pilot, index: int) -> None:
    """Puts the sidebar cursor on one project, which is what the project keys
    act on: the row you are pointing at, not the one whose memos are listed."""
    sidebar = pilot.app.query_one("#projects", ListView)
    sidebar.focus()
    sidebar.index = index
    await pilot.pause()


@pytest.mark.asyncio
async def test_shift_r_offers_to_rename_the_project_under_the_cursor(repo):
    # prefilled, because renaming is usually fixing a name, not replacing it
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("R")
        await pilot.pause()

        assert pilot.app.screen.query_one("#project-rename", Input).value == "personal"


@pytest.mark.asyncio
async def test_renaming_a_project_renames_it_in_the_sidebar(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("R")
        await pilot.pause()
        pilot.app.screen.query_one("#project-rename", Input).value = "home"
        await pilot.press("enter")
        await pilot.pause()

        assert not showing(pilot.app, "#project-rename")
        assert labels(pilot.app.query_one("#projects", ListView)) == ["home (1)", "work (1)"]


@pytest.mark.asyncio
async def test_renaming_a_project_to_nothing_is_refused_without_losing_the_modal(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("R")
        await pilot.pause()
        pilot.app.screen.query_one("#project-rename", Input).value = "   "
        await pilot.press("enter")
        await pilot.pause()

        assert showing(pilot.app, "#project-rename")
        assert [str(n.message) for n in pilot.app._notifications] == ["a project needs a name"]


@pytest.mark.asyncio
async def test_renaming_the_project_being_viewed_keeps_its_memos_in_front_of_you(repo):
    # the memos did not go anywhere, so neither should the list of them
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await point_at_project(pilot, 1)
        await pilot.press("R")
        await pilot.pause()
        pilot.app.screen.query_one("#project-rename", Input).value = "client"
        await pilot.press("enter")
        await pilot.pause()

        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]
        assert "we ship on friday" in transcript(pilot)


@pytest.mark.asyncio
async def test_a_renamed_project_is_found_again_under_its_tidied_name(repo):
    # the store tidies the name it is given, so a screen looking for what it
    # just renamed has to look under the tidied one or find an empty list
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await point_at_project(pilot, 1)
        await pilot.press("R")
        await pilot.pause()
        pilot.app.screen.query_one("#project-rename", Input).value = "  client  "
        await pilot.press("enter")
        await pilot.pause()

        assert labels(pilot.app.query_one("#projects", ListView)) == [
            "client (1)",
            "personal (1)",
        ]
        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]


@pytest.mark.asyncio
async def test_emptying_the_other_project_is_refused_without_taking_the_app_down(repo):
    # other is where emptying puts things, so it has nowhere to be emptied into
    repo.create_memo(
        filename="stray.m4a", wav_path="/tmp/s.wav", duration_s=1.0, language="en",
        segments=[Segment(0, 1000, "hello", speaker="S1")],
        speakers=[Speaker("S1")], project="other",
    )

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("X")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert labels(pilot.app.query_one("#projects", ListView)) == ["other (1)"]
        assert [str(n.message) for n in pilot.app._notifications] == [
            "other is where emptied projects go"
        ]


@pytest.mark.asyncio
async def test_shift_x_asks_before_emptying_a_project(repo):
    # every memo in it is refiled at once, which is not a stray-keypress action
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("X")
        await pilot.pause()

        assert showing(pilot.app, "#confirm-remove")
        assert labels(pilot.app.query_one("#projects", ListView)) == [
            "personal (1)",
            "work (1)",
        ]


@pytest.mark.asyncio
async def test_agreeing_to_empty_a_project_files_its_memos_under_other(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("X")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-remove")
        assert labels(pilot.app.query_one("#projects", ListView)) == ["other (1)", "work (1)"]


@pytest.mark.asyncio
async def test_declining_to_empty_a_project_leaves_every_memo_where_it_was(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("X")
        await pilot.pause()
        assert showing(pilot.app, "#confirm-remove")

        await pilot.press("n")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-remove")
        assert labels(pilot.app.query_one("#projects", ListView)) == [
            "personal (1)",
            "work (1)",
        ]


@pytest.mark.asyncio
async def test_emptying_the_project_being_viewed_stops_showing_its_memos(repo):
    # the project is gone from the sidebar, so the list and panes under it are
    # describing something the screen no longer offers
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await point_at_project(pilot, 1)
        await pilot.press("X")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert labels(pilot.app.query_one("#memos", ListView)) == []
        assert "we ship on friday" not in transcript(pilot)


@pytest.mark.asyncio
async def test_the_project_keys_do_nothing_while_the_memo_list_has_focus(repo):
    # they act on the sidebar cursor, which is not what you are pointing at once
    # you have walked into the memos
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("R")
        await pilot.pause()
        assert not showing(pilot.app, "#project-rename")

        await pilot.press("X")
        await pilot.pause()
        assert not showing(pilot.app, "#confirm-remove")


# --- what state a memo is in ----------------------------------------------


@pytest.mark.asyncio
async def test_pressing_i_says_what_state_the_memo_is_in(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("i")
        await pilot.pause()

        shown = str(pilot.app.screen.query_one("#memo-info", Static).content)
        assert "standup.m4a" in shown
        assert "work" in shown


@pytest.mark.asyncio
async def test_leaving_the_info_puts_the_memo_back_in_front_of_you(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("i")
        await pilot.pause()
        assert showing(pilot.app, "#memo-info")

        await pilot.press("escape")
        await pilot.pause()

        assert not showing(pilot.app, "#memo-info")
        assert "we ship on friday" in transcript(pilot)


@pytest.mark.asyncio
async def test_pressing_i_before_choosing_a_memo_does_nothing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("i")
        await pilot.pause()

        assert not showing(pilot.app, "#memo-info")


# --- finding memos by tag -------------------------------------------------


async def search_tag(pilot, tag: str) -> None:
    """Runs a tag search the way a person does, from whatever is on screen."""
    await pilot.press("/")
    await pilot.pause()
    pilot.app.screen.query_one("#tag-search", Input).value = tag
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_pressing_slash_asks_which_tag_to_look_for(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()

        assert showing(pilot.app, "#tag-search")


@pytest.mark.asyncio
async def test_a_tag_search_reaches_across_every_project(repo):
    # the whole point of it: the sidebar can only ever show you one project
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("personal")
        await pilot.pause()
        await search_tag(pilot, "release")

        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]


@pytest.mark.asyncio
async def test_the_screen_says_which_tag_it_is_showing(repo):
    # the memo list no longer matches the highlighted project, so leaving the
    # screen silent about it would make the sidebar a lie
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "release")

        assert pilot.app.sub_title == "tag: release"


@pytest.mark.asyncio
async def test_leaving_a_tag_search_puts_the_project_back_in_the_list(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("personal")
        await pilot.pause()
        await search_tag(pilot, "release")
        assert labels(pilot.app.query_one("#memos", ListView)) == ["standup.m4a"]

        await pilot.press("escape")
        await pilot.pause()

        assert labels(pilot.app.query_one("#memos", ListView)) == ["shopping.m4a"]
        assert pilot.app.sub_title == ""


@pytest.mark.asyncio
async def test_a_tag_nothing_carries_shows_an_empty_list_rather_than_everything(repo):
    # finding nothing is an answer; falling back to the whole listing is not
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "nobody-used-this")

        assert labels(pilot.app.query_one("#memos", ListView)) == []


@pytest.mark.asyncio
async def test_a_tag_of_nothing_is_refused_without_losing_the_modal(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "   ")

        assert showing(pilot.app, "#tag-search")
        assert [str(n.message) for n in pilot.app._notifications] == ["a tag needs some text"]


# --- work that takes long enough to happen off the main thread ------------


async def finish_jobs(pilot) -> None:
    """Waits for the background work to finish and the screen to catch up."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def said(pilot) -> list[str]:
    """Everything the screen has told the user so far."""
    return [str(n.message) for n in pilot.app._notifications]


@pytest.mark.asyncio
async def test_pressing_x_extracts_notes_for_the_memo_on_screen(repo, monkeypatch):
    # the fake writes through the connection the worker opened, so this exercises
    # the real cross-thread path and not just the key binding
    _work, home = seed(repo)

    def extract(worker_repo, memo_id, force=False):
        worker_repo.save_extraction(memo_id, "claude", {**NOTES, "title": "Shopping list"})
        return "claude"

    monkeypatch.setattr(services, "run_extraction", extract)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, home)
        await pilot.press("x")
        await finish_jobs(pilot)

        assert said(pilot) == ["extracting memo 2 …", "memo 2 extracted via claude"]
        assert "Shopping list" in pilot.app.query_one("#notes", Markdown).source


@pytest.mark.asyncio
async def test_pressing_p_repairs_the_transcript(repo, monkeypatch):
    work, _home = seed(repo)

    def refine(worker_repo, memo_id, dry_run=False):
        (stored,) = worker_repo.segments(memo_id)
        worker_repo.update_refinements(memo_id, {stored.id: "We ship on Friday."})
        return services.RefineResult(changes=[], flagged=[], untouched=0)

    monkeypatch.setattr(services, "refine_transcript", refine)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("p")
        await finish_jobs(pilot)

        assert said(pilot) == ["repairing memo 1 …", "memo 1 repaired"]
        assert "We ship on Friday." in transcript(pilot)


@pytest.mark.asyncio
async def test_pressing_d_runs_speaker_detection_again(repo, monkeypatch):
    work, _home = seed(repo)
    monkeypatch.setattr(services, "rediarize", lambda _repo, _id, log=None: ["S1", "S2"])

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("d")
        await finish_jobs(pilot)

        assert said(pilot) == ["diarizing memo 1 …", "memo 1 diarized"]


@pytest.mark.asyncio
async def test_a_second_job_on_one_memo_is_refused_rather_than_queued(repo, monkeypatch):
    # two passes over the same memo would race each other's writes
    work, _home = seed(repo)
    holding = threading.Event()

    def slow(_repo, _memo_id, force=False):
        holding.wait(timeout=5)
        return "claude"

    monkeypatch.setattr(services, "run_extraction", slow)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()

        assert said(pilot) == ["extracting memo 1 …", "memo 1 is already busy"]

        holding.set()
        await finish_jobs(pilot)


@pytest.mark.asyncio
async def test_two_memos_can_be_worked_on_at_the_same_time(repo, monkeypatch):
    # one job per memo is the rule, not one job at a time
    work, home = seed(repo)
    holding = threading.Event()

    def slow(_repo, _memo_id, force=False):
        holding.wait(timeout=5)
        return "claude"

    monkeypatch.setattr(services, "run_extraction", slow)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()
        await open_memo(pilot, home)
        await pilot.press("x")
        await pilot.pause()

        assert pilot.app.jobs == {work, home}
        assert "memo 2 is already busy" not in said(pilot)

        holding.set()
        await finish_jobs(pilot)


@pytest.mark.asyncio
async def test_a_gateway_that_is_down_says_so_instead_of_taking_the_app_down(repo, monkeypatch):
    # a worker that raises kills the whole app, so the known failures are caught
    work, _home = seed(repo)

    def unavailable(_repo, _memo_id, force=False):
        raise GatewayError("ollama is not running")

    monkeypatch.setattr(services, "run_extraction", unavailable)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await finish_jobs(pilot)

        assert said(pilot) == ["extracting memo 1 …", "ollama is not running"]
        assert pilot.app.jobs == set()


@pytest.mark.asyncio
async def test_a_memo_can_be_worked_on_again_once_its_job_has_finished(repo, monkeypatch):
    work, _home = seed(repo)
    runs: list[int] = []

    def extract(_repo, memo_id, force=False):
        runs.append(memo_id)
        return "claude"

    monkeypatch.setattr(services, "run_extraction", extract)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await finish_jobs(pilot)
        await pilot.press("x")
        await finish_jobs(pilot)

        assert runs == [work, work]
        assert "memo 1 is already busy" not in said(pilot)


@pytest.mark.asyncio
async def test_extracting_over_a_note_somebody_wrote_asks_first(repo, monkeypatch):
    # an afternoon of editing must not go under a keypress
    work, _home = seed(repo)
    repo.save_notes_md(work, "# My own words")
    monkeypatch.setattr(services, "run_extraction", lambda _r, _i, force=False: "claude")

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()

        assert showing(pilot.app, "#confirm-force")
        assert pilot.app.jobs == set()


@pytest.mark.asyncio
async def test_agreeing_to_overwrite_extracts_over_the_edit(repo, monkeypatch):
    work, _home = seed(repo)
    repo.save_notes_md(work, "# My own words")
    seen: dict = {}

    def extract(_repo, _memo_id, force=False):
        seen["force"] = force
        return "claude"

    monkeypatch.setattr(services, "run_extraction", extract)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await finish_jobs(pilot)

        assert seen == {"force": True}


@pytest.mark.asyncio
async def test_declining_to_overwrite_runs_nothing_at_all(repo, monkeypatch):
    work, _home = seed(repo)
    repo.save_notes_md(work, "# My own words")
    ran = []
    monkeypatch.setattr(
        services, "run_extraction", lambda _r, _i, force=False: ran.append(force)
    )

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()
        assert showing(pilot.app, "#confirm-force")

        await pilot.press("n")
        await finish_jobs(pilot)

        assert ran == []
        assert services.notes_markdown(repo, work) == "# My own words"


@pytest.mark.asyncio
async def test_work_finishing_on_a_memo_you_have_left_does_not_redraw_it(repo, monkeypatch):
    # the panes are showing another memo by then; refreshing would yank it away
    work, home = seed(repo)

    # held open until the memo has been left, or the job finishes first and the
    # test proves nothing about what happens to somebody who walked away
    holding = threading.Event()

    def extract(worker_repo, memo_id, force=False):
        holding.wait(timeout=5)
        # a title the seeded notes do not already carry, or this proves nothing
        worker_repo.save_extraction(memo_id, "claude", {**NOTES, "title": "Fresh off the model"})
        return "claude"

    monkeypatch.setattr(services, "run_extraction", extract)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("x")
        await pilot.pause()
        await open_memo(pilot, home)

        holding.set()
        await finish_jobs(pilot)

        # it ran and it landed; what it must not do is pull the panes back
        assert "Fresh off the model" in services.notes_markdown(repo, work)
        assert "buy milk" in transcript(pilot)
        assert "Fresh off the model" not in pilot.app.query_one("#notes", Markdown).source


@pytest.mark.asyncio
async def test_pressing_x_before_choosing_a_memo_does_nothing(repo, monkeypatch):
    seed(repo)
    monkeypatch.setattr(services, "run_extraction", lambda _r, _i, force=False: "claude")

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("x")
        await finish_jobs(pilot)

        assert said(pilot) == []


# --- asking a memo a question ---------------------------------------------


async def ask_question(pilot, question: str) -> None:
    """Puts a question to the memo on screen, the way a person does."""
    await pilot.press("a")
    await pilot.pause()
    pilot.app.screen.query_one("#ask-question", Input).value = question
    await pilot.press("enter")


def answer_shown(pilot) -> str:
    """The answer as it reads on screen."""
    return pilot.app.screen.query_one("#answer", Markdown).source


@pytest.mark.asyncio
async def test_pressing_a_asks_what_you_want_to_know(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("a")
        await pilot.pause()

        assert showing(pilot.app, "#ask-question")


@pytest.mark.asyncio
async def test_the_answer_comes_back_on_screen(repo, monkeypatch):
    work, _home = seed(repo)
    monkeypatch.setattr(services, "ask", lambda _r, _i, q: ("claude", f"You asked: {q}"))

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await ask_question(pilot, "When do they ship?")
        await finish_jobs(pilot)

        assert "You asked: When do they ship?" in answer_shown(pilot)
        assert said(pilot) == ["answering memo 1 …", "memo 1 answered via claude"]


@pytest.mark.asyncio
async def test_the_answer_says_which_memo_and_which_question_it_answers(repo, monkeypatch):
    # it arrives after a wait and outlives the modal that asked, so an answer
    # with no question on it is a paragraph nobody can place
    work, _home = seed(repo)
    monkeypatch.setattr(services, "ask", lambda _r, _i, _q: ("claude", "On Friday."))

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await ask_question(pilot, "When do they ship?")
        await finish_jobs(pilot)

        shown = answer_shown(pilot)
        assert "memo 1" in shown
        assert "When do they ship?" in shown


@pytest.mark.asyncio
async def test_leaving_the_answer_puts_the_memo_back_in_front_of_you(repo, monkeypatch):
    work, _home = seed(repo)
    monkeypatch.setattr(services, "ask", lambda _r, _i, _q: ("claude", "On Friday."))

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await ask_question(pilot, "When do they ship?")
        await finish_jobs(pilot)
        assert showing(pilot.app, "#answer")

        await pilot.press("escape")
        await pilot.pause()

        assert not showing(pilot.app, "#answer")
        assert "we ship on friday" in transcript(pilot)


@pytest.mark.asyncio
async def test_a_question_of_nothing_is_refused_without_losing_the_modal(repo, monkeypatch):
    # refused while the modal is still open, like every other thing typed into
    # one: finding out after it closed would mean typing the question again
    work, _home = seed(repo)
    asked: list = []
    monkeypatch.setattr(
        services, "ask", lambda _r, _i, q: asked.append(q) or ("claude", "…")
    )

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await ask_question(pilot, "   ")
        await finish_jobs(pilot)

        assert showing(pilot.app, "#ask-question")
        assert said(pilot) == ["a question needs something in it"]
        assert asked == []


@pytest.mark.asyncio
async def test_leaving_the_question_unasked_asks_nothing(repo, monkeypatch):
    work, _home = seed(repo)
    asked: list = []
    monkeypatch.setattr(
        services, "ask", lambda _r, _i, q: asked.append(q) or ("claude", "…")
    )

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await pilot.press("a")
        await pilot.pause()
        assert showing(pilot.app, "#ask-question")

        await pilot.press("escape")
        await finish_jobs(pilot)

        assert not showing(pilot.app, "#ask-question")
        assert asked == []


@pytest.mark.asyncio
async def test_an_answer_arrives_even_once_you_have_moved_on(repo, monkeypatch):
    # unlike a repair, an answer is kept nowhere: not showing it loses it, so it
    # arrives labelled rather than being dropped for being late
    work, home = seed(repo)
    holding = threading.Event()

    def slow_answer(_repo, _memo_id, _question):
        holding.wait(timeout=5)
        return ("claude", "On Friday.")

    monkeypatch.setattr(services, "ask", slow_answer)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        await ask_question(pilot, "When do they ship?")
        await pilot.pause()
        await open_memo(pilot, home)

        holding.set()
        await finish_jobs(pilot)

        assert "memo 1" in answer_shown(pilot)


@pytest.mark.asyncio
async def test_pressing_a_before_choosing_a_memo_does_nothing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()

        assert not showing(pilot.app, "#ask-question")
