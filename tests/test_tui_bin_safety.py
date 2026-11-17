"""The TUI's bin dialogs: only an explicit yes destroys, and what is destroyed is
what the dialog named."""

import pytest
from test_tui import (
    MemoApp,
    memo_names,
    open_bin,
    point_at_memo,
    said,
    seed,
    showing,
)

from voice_to_note import services
from voice_to_note.storage.repository import Repository

pytestmark = pytest.mark.ui


@pytest.mark.asyncio
async def test_enter_on_the_delete_for_good_dialog_keeps_the_memo(repo):
    work, _home = seed(repo)
    services.delete_memo(repo, work)

    async with MemoApp(repo).run_test() as pilot:
        await open_bin(pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-delete")
        assert repo.memo(work) is not None
        assert memo_names(pilot.app) == ["standup.m4a"]


@pytest.mark.asyncio
async def test_enter_on_the_empty_the_bin_dialog_keeps_everything(repo):
    work, _home = seed(repo)
    services.delete_memo(repo, work)

    async with MemoApp(repo).run_test() as pilot:
        await open_bin(pilot)
        await pilot.app.run_action("empty_bin")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert not showing(pilot.app, "#confirm-empty-bin")
        assert repo.memo(work) is not None


@pytest.mark.asyncio
async def test_a_yes_to_deleting_for_good_spares_a_memo_restored_while_the_dialog_was_up(repo):
    work, _home = seed(repo)
    services.delete_memo(repo, work)

    async with MemoApp(repo).run_test() as pilot:
        await open_bin(pilot)
        await pilot.press("delete")
        await pilot.pause()
        outside = Repository(repo.path)
        try:
            services.restore_memo(outside, work)
        finally:
            outside.close()
        await pilot.press("y")
        await pilot.pause()

        assert repo.memo(work) is not None
        assert any("restored meanwhile" in line for line in said(pilot))


@pytest.mark.asyncio
async def test_a_yes_to_emptying_the_bin_keeps_what_was_binned_after_the_question(repo):
    work, home = seed(repo)
    services.delete_memo(repo, work)

    async with MemoApp(repo).run_test() as pilot:
        await open_bin(pilot)
        await pilot.app.run_action("empty_bin")
        await pilot.pause()
        outside = Repository(repo.path)
        try:
            services.delete_memo(outside, home)
        finally:
            outside.close()
        await pilot.press("y")
        await pilot.pause()

        assert repo.memo(work) is None
        assert repo.memo(home).deleted_at is not None
        assert any("1 binned since were kept" in line for line in said(pilot))


@pytest.mark.asyncio
async def test_deleting_for_good_a_memo_with_no_audio_file_does_not_crash_the_screen(repo):
    video = repo.create_memo(
        filename="talk", wav_path="", duration_s=1.0, language="en", segments=[],
        project="work", source_url="https://www.youtube.com/watch?v=x",
    )
    services.delete_memo(repo, video)

    async with MemoApp(repo).run_test() as pilot:
        await open_bin(pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert repo.memo(video) is None


@pytest.mark.asyncio
async def test_a_deleted_memos_id_leaves_the_undo_stack_when_it_is_deleted_for_good(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        await pilot.press("delete")
        await pilot.pause()
        assert pilot.app.deleted_here == [work]
        await open_bin(pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

        assert pilot.app.deleted_here == []


@pytest.mark.asyncio
async def test_an_id_purged_by_another_process_is_forgotten_by_the_undo_stack(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        await pilot.press("delete")
        await pilot.pause()
        outside = Repository(repo.path)
        try:
            services.purge_memo(outside, work)
        finally:
            outside.close()
        pilot.app._take_in_outside_writes()
        await pilot.pause()

        assert pilot.app.deleted_here == []


@pytest.mark.asyncio
async def test_a_memo_with_work_running_on_it_is_not_sent_to_the_bin(repo):
    work, _home = seed(repo)

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        pilot.app.jobs.add(work)
        await pilot.press("delete")
        await pilot.pause()

        assert repo.memo(work).deleted_at is None
        assert any("being worked on" in line for line in said(pilot))


@pytest.mark.asyncio
async def test_a_memo_still_recording_is_refused_a_delete_with_a_line_saying_why(repo):
    work, _home = seed(repo)
    repo.con.execute("UPDATE memos SET status='recording' WHERE id=?", (work,))
    repo.con.commit()

    async with MemoApp(repo).run_test() as pilot:
        await point_at_memo(pilot, "work")
        await pilot.press("delete")
        await pilot.pause()

        assert repo.memo(work).deleted_at is None
        assert any("still recording" in line for line in said(pilot))
