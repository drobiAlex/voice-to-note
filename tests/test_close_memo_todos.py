"""Checking off everything one memo still has open, in one move: the service,
the command line and the to-do board and memo menu in the TUI."""

import pytest
from test_cli import add_memo as cli_add_memo
from test_cli import run
from test_tui import (
    MemoApp,
    board_tasks,
    committed,
    from_menu,
    point_at_memo,
    seed,
)

from voice_to_note import services
from voice_to_note.storage.repository import Repository

# --- the service ------------------------------------------------------------


def two_memos(repo) -> tuple[int, int]:
    """Two memos, three tasks on the first and one on the second."""
    first = cli_add_memo(repo, filename="sprint.m4a")
    second = cli_add_memo(repo, filename="retro.m4a")
    committed(repo, first, "Cut the release", "Tag the build", "Mail the team")
    committed(repo, second, "Book the room")
    return first, second


def statuses(repo, memo_id: int) -> dict[str, str]:
    """Each of a memo's tasks and whether it is open or done, read through a
    connection of its own: a command closes the one it was handed."""
    with Repository(repo.path) as fresh:
        return {t.text: t.status for t in fresh.todos(include_done=True, memo_id=memo_id)}


def test_closing_a_memos_to_dos_closes_only_that_memos_open_ones(repo):
    first, second = two_memos(repo)
    done_before = next(t for t in repo.todos(memo_id=first) if t.text == "Tag the build")
    services.set_todo_status(repo, done_before.id, "done")

    closed = services.close_memo_todos(repo, first)

    assert len(closed) == 2
    assert done_before.id not in closed
    assert set(statuses(repo, first).values()) == {"done"}
    assert statuses(repo, second) == {"Book the room": "open"}


def test_closed_to_dos_are_marked_touched_so_a_re_extraction_does_not_bring_them_back(repo):
    first, _second = two_memos(repo)
    services.close_memo_todos(repo, first)

    repo.sync_todos(first, [{"task": "Mail the team", "owner": "Alice", "deadline": ""}])

    assert statuses(repo, first)["Mail the team"] == "done"
    assert all(t.status == "done" for t in repo.todos(include_done=True, memo_id=first))


def test_reopening_restores_exactly_the_rows_that_were_closed(repo):
    first, _second = two_memos(repo)
    done_before = next(t for t in repo.todos(memo_id=first) if t.text == "Tag the build")
    services.set_todo_status(repo, done_before.id, "done")
    closed = services.close_memo_todos(repo, first)

    reopened = services.reopen_todos(repo, closed)

    assert sorted(reopened) == sorted(closed)
    assert statuses(repo, first) == {
        "Cut the release": "open",
        "Tag the build": "done",
        "Mail the team": "open",
    }


def test_closing_a_memo_with_nothing_open_reports_zero(repo):
    first, _second = two_memos(repo)
    services.close_memo_todos(repo, first)

    assert services.close_memo_todos(repo, first) == []


def test_closing_the_to_dos_of_a_binned_memo_is_refused(repo):
    first, _second = two_memos(repo)
    services.delete_memo(repo, first)

    with pytest.raises(services.NotFound):
        services.close_memo_todos(repo, first)


def test_the_notes_box_is_ticked_for_every_task_a_close_checked_off(repo):
    memo_id, _second = two_memos(repo)
    repo.save_extraction(
        memo_id,
        "claude",
        {
            "title": "Sprint",
            "summary": "s",
            "decisions": [],
            "key_insights": [],
            "open_questions": [],
            "dates": [],
            "tags": [],
            "action_items": [
                {"task": "Cut the release", "owner": "Alice", "deadline": "Friday"}
            ],
        },
    )
    assert "[ ] Cut the release" in services.notes(repo, memo_id)

    services.close_memo_todos(repo, memo_id)

    assert "[x] Cut the release" in services.notes(repo, memo_id)


# --- the command line -------------------------------------------------------


def test_todo_done_with_a_memo_closes_its_open_to_dos_and_prints_their_ids(
    repo, monkeypatch, capsys
):
    first, second = two_memos(repo)

    run(monkeypatch, repo, "todo", "done", "--memo", str(first))

    captured = capsys.readouterr()
    with Repository(repo.path) as fresh:
        ids = sorted(t.id for t in fresh.todos(include_done=True, memo_id=first))
    assert sorted(int(i) for i in captured.out.split()) == ids
    assert captured.err == f"closed 3 to-dos of memo {first}\n"
    assert statuses(repo, second) == {"Book the room": "open"}


def test_todo_open_takes_several_ids_so_a_close_can_be_put_back(repo, monkeypatch, capsys):
    first, _second = two_memos(repo)
    run(monkeypatch, repo, "todo", "done", "--memo", str(first))
    ids = capsys.readouterr().out.split()

    run(monkeypatch, Repository(repo.path), "todo", "open", *ids)

    assert set(statuses(repo, first).values()) == {"open"}
    assert capsys.readouterr().err == "3 to-dos back on the list\n"


def test_todo_done_with_nothing_open_says_zero_and_prints_no_ids(repo, monkeypatch, capsys):
    memo_id = cli_add_memo(repo)

    run(monkeypatch, repo, "todo", "done", "--memo", str(memo_id))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"closed 0 to-dos of memo {memo_id}\n"


def test_todo_done_wants_exactly_one_of_an_id_or_a_memo(repo, monkeypatch):
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, repo, "todo", "done")

    assert err.value.code == "name one to-do id, or --memo with a memo id"


def test_todo_done_for_a_binned_memo_is_a_one_line_error(repo, monkeypatch):
    first, _second = two_memos(repo)
    services.delete_memo(repo, first)

    with pytest.raises(SystemExit) as err:
        run(monkeypatch, repo, "todo", "done", "--memo", str(first))

    assert str(err.value.code).count("\n") == 0
    assert str(first) in str(err.value.code)


# --- the board and the memo menu ---------------------------------------------


def notices(pilot) -> list[str]:
    """What the app has said, oldest first."""
    return [str(n.message) for n in pilot.app._notifications]


async def open_board(pilot) -> None:
    """Goes to the board of every memo's to-dos."""
    await from_menu(pilot, "g", "t")
    await pilot.pause()


@pytest.mark.ui
@pytest.mark.asyncio
async def test_x_on_the_board_checks_off_every_open_task_of_the_highlighted_tasks_memo(repo):
    work, home = seed(repo)
    committed(repo, work, "Cut the release", "Tag the build")
    committed(repo, home, "Buy milk")

    async with MemoApp(repo).run_test() as pilot:
        await open_board(pilot)
        await pilot.press("x")
        await pilot.pause()

        assert board_tasks(pilot) == ["Buy milk"]
        assert notices(pilot) == ['closed 2 to-dos of "standup.m4a" — u to undo']
        assert statuses(repo, home) == {"Buy milk": "open"}


@pytest.mark.ui
@pytest.mark.asyncio
async def test_u_on_the_board_reopens_only_what_the_close_checked_off(repo):
    work, _home = seed(repo)
    committed(repo, work, "Cut the release", "Tag the build")
    earlier = next(t for t in repo.todos(memo_id=work) if t.text == "Cut the release")
    services.set_todo_status(repo, earlier.id, "done")

    async with MemoApp(repo).run_test() as pilot:
        await open_board(pilot)
        await pilot.press("x")
        await pilot.pause()
        assert board_tasks(pilot) == []
        await pilot.press("u")
        await pilot.pause()

        assert statuses(repo, work) == {"Cut the release": "done", "Tag the build": "open"}
        assert notices(pilot)[-1] == 'reopened 1 to-do of "standup.m4a"'
        assert board_tasks(pilot) == ["Tag the build"]


@pytest.mark.ui
@pytest.mark.asyncio
async def test_x_with_nothing_open_says_so_and_changes_nothing(repo):
    work, _home = seed(repo)
    committed(repo, work, "Cut the release")
    services.close_memo_todos(repo, work)

    async with MemoApp(repo).run_test() as pilot:
        await open_board(pilot)
        # the board hides finished tasks, so reach one by showing them
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()

        assert notices(pilot) == ['no open to-dos in "standup.m4a"']
        assert statuses(repo, work) == {"Cut the release": "done"}


@pytest.mark.ui
@pytest.mark.asyncio
async def test_u_on_the_board_with_nothing_closed_says_there_is_nothing_to_undo(repo):
    work, _home = seed(repo)
    committed(repo, work, "Cut the release")

    async with MemoApp(repo).run_test() as pilot:
        await open_board(pilot)
        await pilot.press("u")
        await pilot.pause()

        assert notices(pilot) == ["nothing to undo"]
        assert statuses(repo, work) == {"Cut the release": "open"}


@pytest.mark.ui
@pytest.mark.asyncio
async def test_the_board_footer_offers_the_key_that_checks_a_memo_off(repo):
    work, _home = seed(repo)
    committed(repo, work, "Cut the release")

    async with MemoApp(repo).run_test() as pilot:
        await open_board(pilot)

        offered = {
            binding.key: binding.description
            for binding in pilot.app.screen.active_bindings.values()
            for binding in [binding.binding]
            if binding.show
        }
        assert offered["x"] == "Check off all of memo"


@pytest.mark.ui
@pytest.mark.asyncio
async def test_the_memo_menu_checks_off_the_pointed_at_memos_tasks_and_u_takes_it_back(repo):
    work, home = seed(repo)
    committed(repo, work, "Cut the release", "Tag the build")
    committed(repo, home, "Buy milk")

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        await from_menu(pilot, "space", "x")
        assert notices(pilot) == ['closed 2 to-dos of "standup.m4a" — u to undo']
        assert statuses(repo, home) == {"Buy milk": "open"}

        await pilot.press("u")
        await pilot.pause()

        assert set(statuses(repo, work).values()) == {"open"}
        assert notices(pilot)[-1] == 'reopened 2 to-dos of "standup.m4a"'


@pytest.mark.ui
@pytest.mark.asyncio
async def test_u_takes_back_whichever_of_a_delete_and_a_close_came_last(repo):
    work, home = seed(repo)
    committed(repo, home, "Buy milk")

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        await pilot.press("delete")
        await pilot.pause()
        await point_at_memo(pilot, "personal")
        await from_menu(pilot, "space", "x")
        assert statuses(repo, home) == {"Buy milk": "done"}

        await pilot.press("u")
        await pilot.pause()
        assert statuses(repo, home) == {"Buy milk": "open"}
        assert services.memo_is_binned(repo, work)

        await pilot.press("u")
        await pilot.pause()
        assert not services.memo_is_binned(repo, work)
