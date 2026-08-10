import inspect
import threading
from pathlib import Path

import pytest
from textual.widgets import (
    DataTable,
    DirectoryTree,
    Input,
    Label,
    ListView,
    Markdown,
    MarkdownViewer,
    Static,
    TabbedContent,
    TextArea,
    Tree,
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


def memo_table(app) -> DataTable:
    """The memo list, which lays each memo's state out in columns."""
    return app.query_one("#memos", DataTable)


def memo_columns(app) -> list[str]:
    """What the table calls each of its columns, left to right."""
    return [str(column.label) for column in memo_table(app).columns.values()]


def memo_rows(app) -> list[list[str]]:
    """Every row as a person reads it, in the order they are shown."""
    table = memo_table(app)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


def memo_names(app) -> list[str]:
    """The recordings the list is offering, read down its first column."""
    return [row[0] for row in memo_rows(app)]


def row_for(app, name: str) -> dict[str, str]:
    """One memo's row read under the headings above it, so an assertion names
    the column a value is in rather than counting to it."""
    columns = memo_columns(app)
    return next(
        dict(zip(columns, row, strict=True)) for row in memo_rows(app) if row[0] == name
    )


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

        assert memo_names(pilot.app) == ["standup.m4a"]


@pytest.mark.asyncio
async def test_choosing_a_memo_shows_its_notes_and_its_transcript(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()

        assert "Sprint sync" in notes_pane(pilot).document.source
        assert "we ship on friday" in str(pilot.app.query_one("#transcript", Static).content)


@pytest.mark.asyncio
async def test_a_memo_nobody_extracted_says_so_instead_of_failing(repo):
    _work, home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(home)
        await pilot.pause()

        assert "no notes" in notes_pane(pilot).document.source.lower()
        assert "buy milk" in str(pilot.app.query_one("#transcript", Static).content)


@pytest.mark.asyncio
async def test_the_first_project_opens_without_a_keypress_to_wake_it(repo):
    # a sidebar highlighting nothing makes the first Enter of every session do
    # nothing at all, which reads as the app being broken
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("enter")

        assert memo_names(pilot.app) == ["shopping.m4a"]


@pytest.mark.asyncio
async def test_the_keyboard_walks_on_to_the_next_project(repo):
    # the sidebar is focused on open, so every project is reachable without a mouse
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await pilot.press("down", "enter")

        assert memo_names(pilot.app) == ["standup.m4a"]


def test_the_tui_leaves_database_reads_and_formatting_to_services():
    # same rule as cli.py: the screen may hold a Repository, never query one
    source = Path(inspect.getsourcefile(tui_app)).read_text()

    assert "repo." not in source
    assert "from ..transforms" not in source


# --- finding your way around a long note ----------------------------------

LONG_NOTE = "\n\n".join(
    [
        "# Sprint sync",
        "Jump to the [decisions](#decisions).",
        *(f"We talked about item {i}." for i in range(40)),
        "## Decisions",
        "- Ship on Friday",
    ]
)


def notes_pane(pilot) -> MarkdownViewer:
    """The pane the notes are read in, which scrolls itself."""
    return pilot.app.query_one("#notes", MarkdownViewer)


def toc_entries(pilot) -> list[str]:
    """The sections offered down the side of the notes, read the way a person
    reads them: the tree draws a numeral in front of every entry, and that
    numeral is decoration rather than part of the heading. A list that is not
    on screen offers nothing, however well filled in it is."""
    contents = notes_pane(pilot).table_of_contents
    if not contents.display:
        return []
    tree = contents.query_one(Tree)
    found: list[str] = []

    def under(node) -> None:
        """Every section nested under this one, in the order they are shown."""
        for child in node.children:
            found.append(str(child.label).split(" ", 1)[1])
            under(child)

    under(tree.root)
    return found


async def click_link(pilot, href: str) -> None:
    """Clicks a link in the notes the way a terminal click reaches it: through
    the paragraph the link is written in."""
    document = notes_pane(pilot).document
    paragraph = document.get_block_class("paragraph_open")
    await document.query(paragraph).first().action_link(href)
    await pilot.pause()
    await pilot.pause()


@pytest.mark.asyncio
async def test_the_notes_pane_lists_the_sections_of_the_note(repo):
    # notes run to several sections, and a pane that only scrolls makes the
    # reader hunt for the one they came for
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()

        assert toc_entries(pilot) == ["Sprint sync", "Action items", "Decisions"]


@pytest.mark.asyncio
async def test_the_sections_listed_follow_the_memo_being_read(repo):
    # the list beside the notes describes the notes: left behind on the last
    # memo it is worse than none, since it points at sections that are not there
    work, _home = seed(repo)
    services.save_notes(repo, work, "# Standup\n\n## What went wrong\n\n- nothing")

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()

        assert toc_entries(pilot) == ["Standup", "What went wrong"]


@pytest.mark.asyncio
async def test_a_link_to_a_section_jumps_to_it_inside_the_note(repo, monkeypatch):
    # the section is off the bottom of a note this long
    work, _home = seed(repo)
    services.save_notes(repo, work, LONG_NOTE)
    opened: list[str] = []
    app = MemoApp(repo)
    monkeypatch.setattr(app, "open_url", lambda url, **_: opened.append(url))

    async with app.run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        assert notes_pane(pilot).scroll_offset.y == 0

        await click_link(pilot, "#decisions")

        assert notes_pane(pilot).scroll_offset.y > 0
        assert opened == []


@pytest.mark.asyncio
async def test_a_link_out_to_the_web_still_opens_out_there(repo, monkeypatch):
    # a note carries whatever the model wrote in it, plain http links included,
    # and reading one as a section of this note goes looking for a file that
    # was never there
    work, _home = seed(repo)
    services.save_notes(repo, work, "# Sprint sync\n\nSee [the plan](https://example.com/plan).")
    opened: list[str] = []
    app = MemoApp(repo)
    monkeypatch.setattr(app, "open_url", lambda url, **_: opened.append(url))

    async with app.run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()

        await click_link(pilot, "https://example.com/plan")

        assert opened == ["https://example.com/plan"]
        assert pilot.app.is_running


@pytest.mark.asyncio
async def test_a_long_note_scrolls_down_as_far_as_its_last_line(repo):
    # a pane taller than the terminal it is drawn on runs out of note while its
    # own last rows are still below the bottom of the screen: scrolled as far as
    # it will go, the end of the note has still never been shown
    work, _home = seed(repo)
    services.save_notes(repo, work, LONG_NOTE)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_memo(work)
        await pilot.pause()
        notes_pane(pilot).scroll_end(animate=False)
        await pilot.pause()

        last_line = notes_pane(pilot).document.children[-1]
        assert pilot.app.screen.region.contains_region(last_line.region)


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
        assert "In my own words" in notes_pane(pilot).document.source
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
    # the screen behind listens for list selections of its own; picking a voice
    # in here is the modal's business and nothing the screen should act on
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

        assert memo_names(pilot.app) == []
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

        assert memo_names(pilot.app) == ["standup.m4a"]
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
        assert memo_names(pilot.app) == ["standup.m4a"]


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

        assert memo_names(pilot.app) == []
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

        assert memo_names(pilot.app) == ["standup.m4a"]


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
        assert memo_names(pilot.app) == ["standup.m4a"]

        await pilot.press("escape")
        await pilot.pause()

        assert memo_names(pilot.app) == ["shopping.m4a"]
        assert pilot.app.sub_title == ""


@pytest.mark.asyncio
async def test_a_tag_nothing_carries_shows_an_empty_list_rather_than_everything(repo):
    # finding nothing is an answer; falling back to the whole listing is not
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "nobody-used-this")

        assert memo_names(pilot.app) == []


@pytest.mark.asyncio
async def test_a_tag_of_nothing_is_refused_without_losing_the_modal(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "   ")

        assert showing(pilot.app, "#tag-search")
        assert [str(n.message) for n in pilot.app._notifications] == ["a tag needs some text"]


# --- work that takes long enough to happen off the main thread ------------


async def finish_jobs(pilot) -> None:
    """Waits for the app's own background work to finish and the screen to catch
    up. Only its own: a DirectoryTree reads folders in workers of its own, and
    waiting on the whole pool while one is mounted either hangs or comes back
    cancelled. Anything the app started is still waited for, so a job that should
    not have run is still caught having run."""
    jobs = [worker for worker in pilot.app.workers if worker.name == "_run_job"]
    if jobs:
        await pilot.app.workers.wait_for_complete(jobs)
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
        assert "Shopping list" in notes_pane(pilot).document.source


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
        assert "Fresh off the model" not in notes_pane(pilot).document.source


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


# --- bringing a new recording in ------------------------------------------


def recording(tmp_path, name: str = "standup.m4a"):
    """An audio file sitting where somebody would have left it."""
    src = tmp_path / name
    src.write_bytes(b"fake audio")
    return src


def stores_a_memo(text: str = "hello there"):
    """A stand-in pipeline that files a memo the way the real one would, through
    whatever connection the worker handed it."""

    def process(worker_repo, src, project="other", log=None):
        memo_id = worker_repo.create_memo(
            filename=src.name, wav_path=f"/tmp/{src.name}.wav", duration_s=1.0,
            language="en", segments=[Segment(0, 1000, text, speaker="S1")],
            speakers=[Speaker("S1")], project=project,
        )
        return services.ProcessResult(memo_id, 1, ["S1"], "en")

    return process


async def open_add_modal(pilot, root) -> None:
    """Opens the add-recording modal with its file tree pointed somewhere the
    test owns. Left at its default the tree would read the real home folder of
    whatever machine is running the suite."""
    pilot.app.browse_root = root
    await pilot.press("o")
    await pilot.pause()


async def process_file(pilot, src, project: str | None = None) -> None:
    """Brings a recording in the way a person does."""
    await open_add_modal(pilot, src.parent)
    pilot.app.screen.query_one("#source-path", Input).value = str(src)
    if project is not None:
        pilot.app.screen.query_one("#source-project", Input).value = project
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_pressing_o_asks_for_a_recording_and_a_project(repo, tmp_path):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert showing(pilot.app, "#source-path")
        assert showing(pilot.app, "#source-project")


@pytest.mark.asyncio
async def test_the_project_starts_on_the_one_being_browsed(repo, tmp_path):
    # a recording is usually another one of whatever you are already looking at
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()
        await open_add_modal(pilot, tmp_path)

        assert pilot.app.screen.query_one("#source-project", Input).value == "work"


@pytest.mark.asyncio
async def test_the_project_falls_back_to_other_before_one_is_chosen(repo, tmp_path):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert pilot.app.screen.query_one("#source-project", Input).value == "other"


@pytest.mark.asyncio
async def test_a_processed_recording_joins_the_project_on_screen(repo, tmp_path, monkeypatch):
    seed(repo)
    monkeypatch.setattr(services, "process_memo", stores_a_memo())

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()
        await process_file(pilot, recording(tmp_path), "work")
        await finish_jobs(pilot)

        assert not showing(pilot.app, "#source-path")
        assert memo_names(pilot.app) == [
            "standup.m4a",
            "standup.m4a",
        ]
        assert labels(pilot.app.query_one("#projects", ListView)) == [
            "personal (1)",
            "work (2)",
        ]


@pytest.mark.asyncio
async def test_a_processed_recording_does_not_pull_you_off_what_you_are_reading(
    repo, tmp_path, monkeypatch
):
    # it is stored and will be there when they go looking; yanking the panes to
    # it is the same rudeness a finished repair would be
    work, _home = seed(repo)
    monkeypatch.setattr(services, "process_memo", stores_a_memo())

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await process_file(pilot, recording(tmp_path), "work")
        await finish_jobs(pilot)

        assert pilot.app.memo_id == work
        assert "we ship on friday" in transcript(pilot)


@pytest.mark.asyncio
async def test_the_stages_of_a_long_job_are_said_as_they_happen(repo, tmp_path, monkeypatch):
    # minutes of converting and transcribing with a silent screen reads as hung
    seed(repo)

    def process(worker_repo, src, project="other", log=None):
        log(f"converting {src.name} …")
        log("transcribing (12s audio) …")
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", process)

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, recording(tmp_path))
        await finish_jobs(pilot)

        assert "converting standup.m4a …" in said(pilot)
        assert "transcribing (12s audio) …" in said(pilot)


@pytest.mark.asyncio
async def test_a_recording_that_is_not_there_is_refused_without_losing_the_modal(
    repo, tmp_path, monkeypatch
):
    seed(repo)
    ran: list = []
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: ran.append(a) or None
    )

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, tmp_path / "nowhere.m4a")
        await finish_jobs(pilot)

        assert showing(pilot.app, "#source-path")
        assert said(pilot) == ["no recording at that path"]
        assert ran == []


@pytest.mark.asyncio
async def test_a_folder_is_refused_without_losing_the_modal(repo, tmp_path, monkeypatch):
    # a directory is a path that exists, which is not the same as a recording
    seed(repo)
    ran: list = []
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: ran.append(a) or None
    )

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, tmp_path)
        await finish_jobs(pilot)

        assert showing(pilot.app, "#source-path")
        assert said(pilot) == ["no recording at that path"]
        assert ran == []


@pytest.mark.asyncio
async def test_a_project_of_nothing_is_refused_without_losing_the_modal(
    repo, tmp_path, monkeypatch
):
    seed(repo)
    ran: list = []
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: ran.append(a) or None
    )

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, recording(tmp_path), "   ")
        await finish_jobs(pilot)

        assert showing(pilot.app, "#source-path")
        assert said(pilot) == ["a project needs a name"]
        assert ran == []


@pytest.mark.asyncio
async def test_one_recording_is_not_brought_in_twice_at_once(repo, tmp_path, monkeypatch):
    # the same file processed twice over would land as two memos of one recording
    seed(repo)
    holding = threading.Event()

    def slow(worker_repo, src, project="other", log=None):
        holding.wait(timeout=5)
        return services.ProcessResult(1, 0, [], "en")

    monkeypatch.setattr(services, "process_memo", slow)
    src = recording(tmp_path)

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, src)
        await pilot.pause()
        await process_file(pilot, src)
        await pilot.pause()

        assert "standup.m4a is already busy" in said(pilot)

        holding.set()
        await finish_jobs(pilot)


@pytest.mark.asyncio
async def test_a_recording_ffmpeg_cannot_read_says_so_instead_of_taking_the_app_down(
    repo, tmp_path, monkeypatch
):
    seed(repo)

    def unreadable(worker_repo, src, project="other", log=None):
        raise GatewayError("ffmpeg could not read that file")

    monkeypatch.setattr(services, "process_memo", unreadable)

    async with MemoApp(repo).run_test() as pilot:
        await process_file(pilot, recording(tmp_path, "notes.txt"))
        await finish_jobs(pilot)

        assert "ffmpeg could not read that file" in said(pilot)
        assert pilot.app.jobs == set()


@pytest.mark.asyncio
async def test_leaving_the_modal_brings_nothing_in(repo, tmp_path, monkeypatch):
    seed(repo)
    ran: list = []
    monkeypatch.setattr(
        services, "process_memo", lambda *a, **k: ran.append(a) or None
    )

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)
        assert showing(pilot.app, "#source-path")

        await pilot.press("escape")
        await finish_jobs(pilot)

        assert not showing(pilot.app, "#source-path")
        assert ran == []


# --- a footer offering only what applies ----------------------------------

# what each key needs before it means anything
MEMO_KEYS = ["e", "i", "m", "r", "t", "x", "p", "d", "a"]
PROJECT_KEYS = ["R", "X"]
ALWAYS_KEYS = ["o", "slash", "q"]


async def footer_keys(pilot) -> list[str]:
    """The keys the footer is actually offering. Read off the rendered footer
    rather than app.active_bindings, which recomputes on every read and so would
    look right even when the footer on screen had gone stale. It pauses first
    because the footer has drawn nothing the instant the app starts, and reading
    it that early comes back empty — which would make every assertion that a key
    is not offered true for the wrong reason."""
    await pilot.pause()
    return [key.key for key in pilot.app.query("FooterKey")]


async def missing(pilot, keys: list[str]) -> list[str]:
    """Which of these the footer is not offering."""
    shown = await footer_keys(pilot)
    return [key for key in keys if key not in shown]


async def offered(pilot, keys: list[str]) -> list[str]:
    """Which of these the footer is offering."""
    shown = await footer_keys(pilot)
    return [key for key in keys if key in shown]


@pytest.mark.asyncio
async def test_the_footer_offers_nothing_needing_a_memo_before_one_is_open(repo):
    # a key that does nothing is a key that reads as broken
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        assert await offered(pilot, MEMO_KEYS) == []
        assert await missing(pilot, ALWAYS_KEYS) == []


@pytest.mark.asyncio
async def test_opening_a_memo_offers_what_can_be_done_to_it(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)

        assert await missing(pilot, MEMO_KEYS) == []


@pytest.mark.asyncio
async def test_the_memo_keys_stay_when_the_cursor_leaves_the_sidebar(repo):
    # they act on the memo being read, not on whatever holds focus, so walking
    # down into the memo list must not take them away
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await open_memo(pilot, work)
        memo_table(pilot.app).focus()
        await pilot.pause()

        assert await missing(pilot, MEMO_KEYS) == []


@pytest.mark.asyncio
async def test_the_project_keys_are_offered_only_while_the_sidebar_has_the_cursor(repo):
    # the asymmetry with the memo keys is the point: these act on the row the
    # cursor is resting on, and off the sidebar there is no such row
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        assert await missing(pilot, PROJECT_KEYS) == []

        memo_table(pilot.app).focus()
        await pilot.pause()

        assert await offered(pilot, PROJECT_KEYS) == []


@pytest.mark.asyncio
async def test_the_way_back_is_offered_only_while_a_tag_search_is_showing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        assert "escape" not in await footer_keys(pilot)

        await search_tag(pilot, "release")

        assert "escape" in await footer_keys(pilot)


@pytest.mark.asyncio
async def test_leaving_a_tag_search_takes_the_way_back_off_the_footer(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("personal")
        await pilot.pause()
        await search_tag(pilot, "release")
        await pilot.press("escape")
        await pilot.pause()

        assert "escape" not in await footer_keys(pilot)


@pytest.mark.asyncio
async def test_a_memo_going_out_of_view_takes_its_keys_off_the_footer(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await open_memo(pilot, work)
        await pilot.press("m")
        await pilot.pause()
        pilot.app.screen.query_one("#project-name", Input).value = "side"
        await pilot.press("enter")
        await pilot.pause()

        assert await offered(pilot, MEMO_KEYS) == []


# --- the guards behind the hidden keys ------------------------------------

# Hiding a key stops the key reaching its action at all, so a test that presses
# it proves nothing about the guard inside. These reach the actions directly,
# which is the only way left to hold the case where one is reached anyway.

MEMO_ACTIONS = [
    "edit_notes", "memo_info", "move_memo", "rename_speaker", "toggle_raw",
    "extract", "repair", "diarize", "ask",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", MEMO_ACTIONS)
async def test_a_memo_action_reached_with_no_memo_open_does_nothing(repo, action):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        getattr(pilot.app, f"action_{action}")()
        await pilot.pause()

        assert len(pilot.app.screen_stack) == 1
        assert pilot.app.jobs == set()
        assert said(pilot) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rename_project", "remove_project"])
async def test_a_project_action_reached_off_the_sidebar_does_nothing(repo, action):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        memo_table(pilot.app).focus()
        await pilot.pause()

        getattr(pilot.app, f"action_{action}")()
        await pilot.pause()

        assert len(pilot.app.screen_stack) == 1
        assert said(pilot) == []


# --- browsing for the recording -------------------------------------------


def one_recording_among(tmp_path, *others: str):
    """A folder holding exactly one recording, plus whatever else is named. One
    recording means the tree has one file to walk to, so a test can reach it by
    what is there rather than by counting rows."""
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    for name in others:
        (tmp_path / name).write_bytes(b"x")
    return src


def tree_labels(pilot) -> list[str]:
    """What the tree is offering to pick from."""
    tree = pilot.app.screen.query_one("#source-tree", DirectoryTree)
    return [str(node.label) for node in tree.root.children]


async def pick_the_recording(pilot) -> None:
    """Walks to the one recording the tree shows and picks it."""
    pilot.app.screen.query_one("#source-tree", DirectoryTree).focus()
    await pilot.pause()
    await pilot.press("down")
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_the_add_modal_offers_somewhere_to_browse(repo, tmp_path):
    seed(repo)
    one_recording_among(tmp_path)

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert showing(pilot.app, "#source-tree")


@pytest.mark.asyncio
async def test_the_tree_starts_in_the_home_folder(repo):
    # where recordings actually are; the tests point it somewhere they own
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        assert pilot.app.browse_root == Path.home()


@pytest.mark.asyncio
async def test_the_tree_shows_recordings_and_folders_and_nothing_else(repo, tmp_path):
    # a folder of holiday photos and receipts is not a list of recordings
    seed(repo)
    one_recording_among(tmp_path, "notes.txt", "photo.png")
    (tmp_path / "last-month").mkdir()

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert tree_labels(pilot) == ["last-month", "standup.m4a"]


@pytest.mark.asyncio
async def test_the_tree_shows_a_recording_whatever_case_its_name_is_in(repo, tmp_path):
    # phones and voice recorders hand back .M4A about as often as .m4a
    seed(repo)
    (tmp_path / "INTERVIEW.M4A").write_bytes(b"fake audio")

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert tree_labels(pilot) == ["INTERVIEW.M4A"]


@pytest.mark.asyncio
async def test_the_tree_shows_qta_recordings(repo, tmp_path):
    # the user's voice recorder hands back .qta files, which still need to walk into the tree
    seed(repo)
    (tmp_path / "status.qta").write_bytes(b"fake audio")

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)

        assert tree_labels(pilot) == ["status.qta"]


@pytest.mark.asyncio
async def test_picking_a_recording_from_the_tree_fills_the_path(repo, tmp_path):
    # the typed line stays the one source of truth; the tree only writes to it
    seed(repo)
    src = one_recording_among(tmp_path)

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)
        await pick_the_recording(pilot)

        assert pilot.app.screen.query_one("#source-path", Input).value == str(src)


@pytest.mark.asyncio
async def test_picking_a_recording_moves_on_to_sending_it(repo, tmp_path):
    # picking is half of it; the project is still there to check before enter
    seed(repo)
    one_recording_among(tmp_path)

    async with MemoApp(repo).run_test() as pilot:
        await open_add_modal(pilot, tmp_path)
        await pick_the_recording(pilot)

        assert pilot.app.screen.focused is pilot.app.screen.query_one("#source-path", Input)


@pytest.mark.asyncio
async def test_a_recording_picked_from_the_tree_goes_the_same_way_as_a_typed_one(
    repo, tmp_path, monkeypatch
):
    # one path in, one set of checks: picking must not skip the validation a
    # typed path goes through
    seed(repo)
    one_recording_among(tmp_path)
    monkeypatch.setattr(services, "process_memo", stores_a_memo())

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()
        await open_add_modal(pilot, tmp_path)
        pilot.app.screen.query_one("#source-project", Input).value = "work"
        await pick_the_recording(pilot)
        await pilot.press("enter")
        await finish_jobs(pilot)

        assert not showing(pilot.app, "#source-path")
        assert memo_names(pilot.app) == [
            "standup.m4a",
            "standup.m4a",
        ]


# --- the memo list as a table of what state each memo is in ---------------


def detailed_memo(repo, filename: str = "interview.m4a", project: str = "work") -> int:
    """A memo with something worth reading in every column: two voices, a minute
    and a quarter of audio, and notes already extracted from it."""
    memo_id = repo.create_memo(
        filename=filename, wav_path="/tmp/i.wav", duration_s=75.0, language="en",
        segments=[Segment(0, 1000, "we talked it over", speaker="S1")],
        speakers=[Speaker("S1"), Speaker("S2")], project=project,
    )
    repo.save_extraction(memo_id, "claude", NOTES)
    return memo_id


@pytest.mark.asyncio
async def test_the_memo_list_shows_each_memos_state_beside_its_name(repo):
    # all of it was behind the info key; the list has the room to just show it
    memo_id = detailed_memo(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()

        assert memo_columns(pilot.app) == [
            "name",
            "duration",
            "speakers",
            "status",
            "created",
            "updated",
        ]
        info = services.memo_info(repo, memo_id)
        assert row_for(pilot.app, "interview.m4a") == {
            "name": "interview.m4a",
            "duration": "75s",
            "speakers": "2",
            "status": "extracted",
            "created": info.created,
            "updated": info.updated,
        }


@pytest.mark.asyncio
async def test_a_repaired_and_rewritten_memo_is_marked_in_the_list(repo):
    # the info modal reports both; on the list they are what tells one memo
    # apart from the one under it
    work, _home = seed(repo)
    repair(repo, work, "We ship on Friday.")
    repo.save_notes_md(work, "# My own words")

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        await pilot.pause()

        assert row_for(pilot.app, "standup.m4a")["status"] == "extracted (repaired, edited)"


@pytest.mark.asyncio
async def test_a_memo_nobody_has_touched_is_marked_with_nothing(repo):
    seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("personal")
        await pilot.pause()

        assert row_for(pilot.app, "shopping.m4a")["status"] == "transcribed"


@pytest.mark.asyncio
async def test_choosing_a_row_opens_the_memo_that_row_is_about(repo):
    # the row carries its memo's id, which is not where it sits in the list: the
    # newest memo is the first row and the highest id
    work, _home = seed(repo)
    later = detailed_memo(repo, filename="retro.m4a")

    async with MemoApp(repo).run_test() as pilot:
        pilot.app.show_project("work")
        memo_table(pilot.app).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.memo_id == later
        assert "we talked it over" in transcript(pilot)

        await pilot.press("down", "enter")
        await pilot.pause()

        assert pilot.app.memo_id == work
        assert "we ship on friday" in transcript(pilot)


@pytest.mark.asyncio
async def test_a_tag_search_fills_the_same_table_with_the_same_columns(repo):
    # the list is one list however it was filled, so a searched-for memo says
    # as much about itself as a browsed one
    detailed_memo(repo)

    async with MemoApp(repo).run_test() as pilot:
        await search_tag(pilot, "release")

        assert row_for(pilot.app, "interview.m4a")["speakers"] == "2"
