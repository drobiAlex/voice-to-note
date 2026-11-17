# ruff: noqa: F811
"""What the bin may and may not destroy: only what was shown and confirmed,
never a live or recording memo, and never by way of an empty audio path."""

import sys

import pytest
from test_bin import at_db, full_memo, seeded, wav  # noqa: F401  (fixtures)

from voice_to_note import services
from voice_to_note.storage.repository import Repository


def bare_memo(repo, name="v.mp4", wav_path="", project="work", status=None) -> int:
    memo_id = repo.create_memo(
        filename=name, wav_path=wav_path, duration_s=1.0, language="en", segments=[],
        project=project,
    )
    if status:
        repo.con.execute("UPDATE memos SET status=? WHERE id=?", (status, memo_id))
        repo.con.commit()
    return memo_id


# --- what was confirmed is what goes ----------------------------------------


def test_emptying_the_bin_removes_only_the_memos_that_were_shown(repo, wav):
    shown = full_memo(repo, wav, "a.m4a")
    late_wav = wav.parent / "late.wav"
    late_wav.write_bytes(b"RIFF")
    late = full_memo(repo, late_wav, "late.m4a")
    services.delete_memo(repo, shown)
    seen = [m.id for m in services.binned_memos(repo)]
    services.delete_memo(repo, late)

    result = services.empty_bin(repo, seen)

    assert (result.purged, result.arrived) == (1, 1)
    assert repo.memo(shown) is None
    assert repo.memo(late).deleted_at is not None and late_wav.exists()


def test_emptying_the_bin_leaves_a_memo_restored_after_it_was_shown(repo, wav):
    memo_id = full_memo(repo, wav)
    services.delete_memo(repo, memo_id)
    seen = [memo_id]
    services.restore_memo(repo, memo_id)

    result = services.empty_bin(repo, seen)

    assert (result.purged, result.restored) == (0, 1)
    assert repo.memo(memo_id) is not None and wav.exists()


def test_the_command_line_keeps_a_memo_binned_while_its_question_was_open(
    at_db, wav, capsys, monkeypatch, tmp_path  # noqa: F811
):
    path, run = at_db
    first = seeded(path, wav)
    other = tmp_path / "late.wav"
    other.write_bytes(b"RIFF")
    with Repository(path) as repo:
        late = full_memo(repo, other, "late.m4a")
    run("delete", str(first))
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def answer() -> str:
        with Repository(path) as elsewhere:
            services.delete_memo(elsewhere, late)
        return "y"

    monkeypatch.setattr("builtins.input", answer)

    run("bin", "empty")

    err = capsys.readouterr().err
    assert "emptied the bin — 1 memo deleted for good" in err
    assert "1 more arrived in the bin meanwhile and were kept" in err
    with Repository(path) as repo:
        assert repo.memo(first) is None
        assert repo.memo(late).deleted_at is not None


def test_purging_for_a_binned_question_does_nothing_to_a_memo_restored_meanwhile(repo, wav):
    memo_id = full_memo(repo, wav)
    services.delete_memo(repo, memo_id)
    services.restore_memo(repo, memo_id)

    with pytest.raises(services.NotInBin, match="restored meanwhile"):
        services.purge_memo(repo, memo_id, only_if_binned=True)

    assert repo.memo(memo_id) is not None and wav.exists()


def test_forever_on_a_live_memo_still_works_where_the_prompt_names_it(repo, wav):
    memo_id = full_memo(repo, wav)

    services.purge_memo(repo, memo_id)

    assert repo.memo(memo_id) is None


# --- audio paths and recordings ---------------------------------------------


def test_a_memo_with_no_audio_path_can_be_deleted_for_good(repo):
    memo_id = bare_memo(repo)

    assert services.purge_memo(repo, memo_id) == "v.mp4"
    assert repo.memo(memo_id) is None


def test_emptying_the_bin_goes_on_past_a_file_that_cannot_be_removed(repo, wav, monkeypatch):
    stuck = wav.parent / "stuck.wav"
    stuck.write_bytes(b"RIFF")
    first = full_memo(repo, stuck, "a.m4a")
    second = full_memo(repo, wav, "b.m4a")
    services.delete_memo(repo, first)
    services.delete_memo(repo, second)
    real_unlink = type(stuck).unlink

    def unlink(self, missing_ok=False):
        if self.name == "stuck.wav":
            raise PermissionError("denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(stuck), "unlink", unlink)
    told: list[str] = []

    result = services.empty_bin(repo, log=told.append)

    assert result.purged == 2
    assert repo.binned() == []
    assert any("stuck.wav" in line and "denied" in line for line in told)


def test_a_memo_being_recorded_cannot_be_binned_or_deleted_for_good(repo):
    memo_id = bare_memo(repo, "meeting", status="recording")

    with pytest.raises(services.InvalidInput, match="still recording"):
        services.delete_memo(repo, memo_id)
    with pytest.raises(services.InvalidInput, match="still recording"):
        services.purge_memo(repo, memo_id)

    assert repo.memo(memo_id).deleted_at is None


# --- projects and the bin ---------------------------------------------------


def test_a_memo_binned_and_then_its_project_renamed_restores_into_the_new_name(repo, wav):
    memo_id = full_memo(repo, wav, project="old")
    live = full_memo(repo, wav, "live.m4a", project="old")
    services.delete_memo(repo, memo_id)
    repo.con.execute("UPDATE memos SET updated_at='2020-01-01 00:00:00' WHERE id=?", (memo_id,))

    assert services.rename_project(repo, "old", "new") == 1
    assert repo.memo(memo_id).updated_at == "2020-01-01 00:00:00"
    services.restore_memo(repo, memo_id)

    assert repo.memo(memo_id).project == "new"
    assert repo.memo(live).project == "new"


def test_emptying_a_project_carries_its_binned_memos_along_and_counts_only_live_ones(repo, wav):
    binned = full_memo(repo, wav, "a.m4a", project="side")
    full_memo(repo, wav, "b.m4a", project="side")
    services.delete_memo(repo, binned)
    repo.con.execute("UPDATE memos SET updated_at='2020-01-01 00:00:00' WHERE id=?", (binned,))

    assert services.remove_project(repo, "side") == 1

    assert repo.memo(binned).project == "other"
    assert repo.memo(binned).updated_at == "2020-01-01 00:00:00"


def test_a_project_holding_only_binned_memos_is_not_found_and_left_alone(repo, wav):
    memo_id = full_memo(repo, wav, project="ghost")
    services.delete_memo(repo, memo_id)

    with pytest.raises(services.NotFound):
        services.rename_project(repo, "ghost", "new")

    assert repo.memo(memo_id).project == "ghost"


# --- flags that mean nothing here -------------------------------------------


def test_the_bin_empty_command_refuses_json_rather_than_ignoring_it(at_db, wav):  # noqa: F811
    path, run = at_db
    memo_id = seeded(path, wav)
    run("delete", str(memo_id))

    with pytest.raises(SystemExit) as raised:
        run("bin", "--json", "empty", "--yes")

    assert "--json" in str(raised.value)
    with Repository(path) as repo:
        assert repo.memo(memo_id) is not None


def test_a_json_flag_the_destructive_commands_do_not_have_is_rejected_by_the_parser(
    at_db, wav  # noqa: F811
):
    path, run = at_db
    memo_id = seeded(path, wav)

    for argv in (["delete", str(memo_id), "--forever", "--json"], ["bin", "empty", "--json"]):
        with pytest.raises(SystemExit) as raised:
            run(*argv)
        assert raised.value.code == 2

    with Repository(path) as repo:
        assert repo.memo(memo_id) is not None
