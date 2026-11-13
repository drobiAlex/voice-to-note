import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from test_record import DENIES_THE_MICROPHONE, REPORTS_LEVELS, helper

from voice_to_note import cli, config, services
from voice_to_note.domain import RunRecord, Segment, StageRecord, Turn
from voice_to_note.gateways import GatewayError, capture, eventlog
from voice_to_note.transforms import metrics
from voice_to_note.transforms.levels import DROPPED_S, LevelWatch


def events(name: str | None = None) -> list[dict]:
    """Every event the log holds, oldest first, or just those of one name."""
    found = [json.loads(line) for line in eventlog.tail(100_000)]
    return [e for e in found if name is None or e["event"] == name]


# --- the event log ---------------------------------------------------------


def test_an_event_is_one_timestamped_json_line():
    eventlog.record("thing.happened", memo=3, why="because")

    (only,) = events()
    assert only["event"] == "thing.happened"
    assert (only["memo"], only["why"]) == (3, "because")
    assert only["ts"].endswith("+00:00")


def test_the_log_never_grows_past_its_cap(monkeypatch):
    # about 3 KiB in all, so a few hundred events have to rotate many times
    monkeypatch.setattr(config, "LOG_MAX_MB", 3 / 1024)
    for n in range(500):
        eventlog.record("tick", n=n, padding="x" * 40)

    cap = config.LOG_MAX_MB * 1024 * 1024
    assert eventlog.size_bytes() <= cap
    assert sorted(p.name for p in config.LOGS_DIR.glob("events*.jsonl")) == [
        "events.1.jsonl",
        "events.2.jsonl",
        "events.jsonl",
    ]


def test_the_oldest_events_are_the_ones_dropped(monkeypatch):
    monkeypatch.setattr(config, "LOG_MAX_MB", 3 / 1024)
    for n in range(500):
        eventlog.record("tick", n=n)

    kept = [e["n"] for e in events("tick")]
    assert kept[-1] == 499
    assert 0 not in kept
    assert kept == sorted(kept)


def test_a_cap_of_nothing_turns_the_log_off(monkeypatch):
    monkeypatch.setattr(config, "LOG_MAX_MB", 0)

    eventlog.record("tick")

    assert not config.LOGS_DIR.exists()


def test_a_huge_field_is_cut_down_to_keep_the_event_one_readable_line():
    eventlog.record("tool.failed", stderr="e" * 50_000)

    (only,) = events()
    assert len(only["stderr"]) < eventlog.FIELD_MAX_CHARS + 50
    assert only["stderr"].endswith("more chars)")


def test_a_log_that_cannot_be_written_never_takes_the_caller_down(monkeypatch, tmp_path):
    blocked = tmp_path / "a-file-not-a-folder"
    blocked.write_text("")
    monkeypatch.setattr(config, "LOGS_DIR", blocked)

    eventlog.record("tick")  # must not raise


def test_the_tail_of_another_log_starts_on_a_line_and_stays_within_its_cap(tmp_path):
    log = tmp_path / "menubar.log"
    log.write_text("".join(f"line {n}\n" for n in range(10_000)))

    tail = eventlog.text_tail(log, 1000)

    assert len(tail.encode()) <= 1000
    assert tail.startswith("line ")
    assert tail.rstrip().endswith("line 9999")


def test_the_tail_of_a_log_that_is_not_there_is_empty(tmp_path):
    assert eventlog.text_tail(tmp_path / "missing.log", 1000) == ""


# --- runs in the database --------------------------------------------------


def test_a_run_comes_back_with_its_stages_in_the_order_they_ended(repo):
    run_id = repo.start_run("process", None, "0.1.0", keep=10)
    repo.add_stage(run_id, "convert", 1.5, 0.2, "ok", {})
    repo.add_stage(run_id, "transcribe", 30.0, 110.0, "ok", {"model": "large-v3-turbo"})
    repo.finish_run(run_id, 32.0, "ok", None, audio_s=600.0, memo_id=7)

    (run,) = repo.runs()
    assert (run.kind, run.memo_id, run.outcome, run.audio_s) == ("process", 7, "ok", 600.0)
    assert [st.stage for st in run.stages] == ["convert", "transcribe"]
    assert run.stages[1].detail == {"model": "large-v3-turbo"}


def test_only_the_newest_runs_are_kept_and_their_stages_with_them(repo):
    for n in range(8):
        run_id = repo.start_run("extract", n, "0.1.0", keep=3)
        repo.add_stage(run_id, "llm", 1.0, None, "ok", {})

    assert [run.memo_id for run in repo.runs()] == [7, 6, 5]
    stages = repo.con.execute("SELECT count(*) FROM run_stages").fetchone()[0]
    assert stages == 3


def test_a_run_outlives_the_memo_it_was_about(repo, tmp_path):
    memo_id = repo.create_memo(
        filename="a.m4a", wav_path=str(tmp_path / "a.wav"), duration_s=1.0, language="en",
        segments=[], speakers=[],
    )
    repo.start_run("process", memo_id, "0.1.0", keep=10)
    repo.delete_memo(memo_id)

    assert [run.memo_id for run in repo.runs()] == [memo_id]


# --- timing the work -------------------------------------------------------


def wired_pipeline(tmp_path, monkeypatch) -> Path:
    """process_memo with every tool faked, a minute of audio long."""
    src = tmp_path / "standup.m4a"
    src.write_bytes(b"fake audio")
    monkeypatch.setattr(services.config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(services.audio, "to_wav16k", lambda _src, _dst: None)
    monkeypatch.setattr(services.audio, "duration_seconds", lambda _path: 60.0)
    monkeypatch.setattr(services.audio, "recorded_at", lambda _path: "2026-01-01T09:00:00Z")
    monkeypatch.setattr(
        services.whisper,
        "transcribe",
        lambda _wav, _duration: {
            "transcription": [{"text": " the launch is friday ", "offsets": {"from": 0, "to": 1000}}],
            "result": {"language": "en"},
        },
    )
    monkeypatch.setattr(services.sherpa, "diarize", lambda _wav, num_speakers=None: [Turn(0, 1000, "S1")])
    monkeypatch.setattr(services.sherpa, "speaker_embeddings", lambda _wav, _turns: {})
    return src


def test_processing_a_recording_times_each_stage_against_the_memo_it_made(
    repo, tmp_path, monkeypatch
):
    src = wired_pipeline(tmp_path, monkeypatch)

    result = services.process_memo(repo, src)

    (run,) = repo.runs()
    assert (run.kind, run.memo_id, run.outcome, run.audio_s) == ("process", result.memo_id, "ok", 60.0)
    assert [st.stage for st in run.stages] == ["convert", "transcribe", "diarize"]
    assert run.stages[1].detail["model"] == config.WHISPER_MODEL
    assert [e["outcome"] for e in events("run.end")] == ["ok"]


def test_a_stage_that_fails_says_where_and_the_failure_still_reaches_the_caller(
    repo, tmp_path, monkeypatch
):
    src = wired_pipeline(tmp_path, monkeypatch)

    def broken(_wav, _duration):
        raise GatewayError("whisper-cli failed: out of memory")

    monkeypatch.setattr(services.whisper, "transcribe", broken)

    with pytest.raises(GatewayError, match="out of memory"):
        services.process_memo(repo, src)

    (run,) = repo.runs()
    assert run.outcome == "failed"
    assert "out of memory" in run.error
    assert [(st.stage, st.outcome) for st in run.stages] == [("convert", "ok"), ("transcribe", "failed")]


def test_an_interrupted_run_is_recorded_as_cancelled(repo, tmp_path, monkeypatch):
    src = wired_pipeline(tmp_path, monkeypatch)

    def interrupted(_wav, _duration):
        raise KeyboardInterrupt

    monkeypatch.setattr(services.whisper, "transcribe", interrupted)

    with pytest.raises(KeyboardInterrupt):
        services.process_memo(repo, src)

    assert repo.runs()[0].outcome == "cancelled"


def test_a_database_that_will_not_take_a_timing_costs_the_timing_not_the_work(
    repo, tmp_path, monkeypatch
):
    src = wired_pipeline(tmp_path, monkeypatch)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "add_stage", locked)

    result = services.process_memo(repo, src)

    assert repo.memo(result.memo_id) is not None
    assert events("metrics.unwritable")


def test_keeping_no_runs_still_logs_the_story(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_KEEP_RUNS", 0)
    src = wired_pipeline(tmp_path, monkeypatch)

    services.process_memo(repo, src)

    assert repo.runs() == []
    assert [e["stage"] for e in events("stage.end")] == ["convert", "transcribe", "diarize"]


def test_a_stage_timed_outside_any_run_lands_in_the_log_alone():
    with services.stage("merge"):
        pass

    (ended,) = events("stage.end")
    assert (ended["stage"], ended["run"], ended["outcome"]) == ("merge", None, "ok")


def test_writing_notes_times_each_backend_it_tried(repo, tmp_path, monkeypatch):
    memo_id = repo.create_memo(
        filename="a.m4a", wav_path=str(tmp_path / "a.wav"), duration_s=1.0, language="en",
        segments=[Segment(0, 1000, "hello", "S1")], speakers=[],
    )
    monkeypatch.setattr(services.llm, "claude_available", lambda: True)
    monkeypatch.setattr(services.llm, "ollama_available", lambda: True)
    monkeypatch.setattr(services.llm, "claude_complete", lambda *_a, **_k: "not json")
    monkeypatch.setattr(
        services.llm,
        "ollama_complete",
        lambda *_a, **_k: json.dumps(
            {
                "title": "t", "summary": "s", "action_items": [], "decisions": [],
                "key_insights": [], "open_questions": [], "dates": [], "tags": [],
            }
        ),
    )
    monkeypatch.setattr(config, "LLM_BACKENDS", "claude,ollama")

    services.run_extraction(repo, memo_id)

    (run,) = repo.runs()
    assert run.kind == "extract"
    assert [(st.detail["backend"], st.outcome) for st in run.stages] == [
        ("claude", "failed"),
        ("ollama", "ok"),
    ]


# --- reading it back -------------------------------------------------------


def run_record(kind, stage_walls, audio_s=None, outcome="ok", failed_stage=None) -> RunRecord:
    """A run as the database would hand it back, for the summaries to chew on."""
    return RunRecord(
        id=1, kind=kind, memo_id=None, started_at="2026-11-01T09:00:00.000Z",
        wall_s=sum(stage_walls.values()), outcome=outcome, error=None, audio_s=audio_s,
        version="0.1.0",
        stages=tuple(
            StageRecord(name, wall, None, "failed" if name == failed_stage else "ok", {})
            for name, wall in stage_walls.items()
        ),
    )


def test_the_percentile_is_always_a_value_that_happened():
    assert metrics.percentile([5.0, 1.0, 3.0], 0.5) == 3.0
    assert metrics.percentile([float(n) for n in range(1, 11)], 0.9) == 9.0
    assert metrics.percentile([7.0], 0.9) == 7.0


def test_stage_timings_are_summarised_per_kind_with_speed_against_the_audio():
    runs = [
        run_record("process", {"transcribe": 60.0}, audio_s=600.0),
        run_record("process", {"transcribe": 30.0}, audio_s=600.0),
        run_record("process", {"transcribe": 2.0}, audio_s=600.0, failed_stage="transcribe"),
    ]

    (st,) = metrics.stage_stats(runs)
    assert (st.kind, st.stage, st.count, st.failed) == ("process", "transcribe", 3, 1)
    # the failed one is counted but kept out of the timings
    assert (st.wall_median, st.wall_max) == (30.0, 60.0)
    assert st.speed_median == 10.0


def test_outcomes_count_how_each_kind_of_run_ended():
    runs = [
        run_record("record", {}, outcome="ok"),
        run_record("record", {}, outcome="cancelled"),
        run_record("record", {}, outcome=None),
    ]

    (o,) = metrics.outcomes(runs)
    assert (o.ok, o.failed, o.cancelled, o.unfinished) == (1, 0, 1, 1)


def test_the_slowest_runs_are_the_longest_waits():
    quick = run_record("extract", {"llm": 3.0})
    slow = run_record("process", {"transcribe": 300.0}, audio_s=3600.0)

    assert metrics.slowest([quick, slow], 1) == [slow]


def test_stats_say_so_when_nothing_has_been_timed(repo):
    assert services.stats_text(repo) == "nothing timed in the last 30 days"


def test_stats_name_the_stage_a_slow_run_spent_its_time_in(repo, tmp_path, monkeypatch):
    services.process_memo(repo, wired_pipeline(tmp_path, monkeypatch))

    shown = services.stats_text(repo)

    assert "| process | transcribe |" in shown
    assert "longest runs" in shown


def test_a_report_carries_timings_and_events_but_nothing_anybody_said(
    repo, tmp_path, monkeypatch
):
    services.process_memo(repo, wired_pipeline(tmp_path, monkeypatch))
    monkeypatch.setattr(config, "MENUBAR_LOG", tmp_path / "menubar.log")
    (tmp_path / "menubar.log").write_text(f"vtn started from {Path.home()}/meetings\n")

    path = services.write_report(repo, tmp_path / "reports")
    text = path.read_text()

    assert "## Timings" in text and "## Recent events" in text and "## Settings" in text
    assert "run.end" in text
    assert "the launch is friday" not in text
    assert str(Path.home()) not in text
    assert "~/meetings" in text


# --- the recorder ----------------------------------------------------------


def test_a_microphone_silent_while_the_call_talks_is_reported_as_dropped():
    watch = LevelWatch(interval_s=1.0)
    changes = [watch.hear(-20.0, -60.0) for _ in range(int(DROPPED_S))]

    assert changes[-1] == [("dropped", "mic", DROPPED_S)]
    assert not any(changes[:-1])

    assert watch.hear(-20.0, -30.0) == [("back", "mic", DROPPED_S)]
    assert watch.summary()["mic_drops"] == 1


def test_a_microphone_silent_while_nobody_talks_is_only_a_pause():
    watch = LevelWatch(interval_s=1.0)

    assert not any(watch.hear(-60.0, -60.0) for _ in range(120))
    assert watch.summary()["mic_drops"] == 0
    assert watch.summary()["mic_silent_s"] == 120.0


def test_what_the_recorder_says_on_stderr_is_kept_in_the_log(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, DENIES_THE_MICROPHONE))

    with pytest.raises(GatewayError):
        capture.start(tmp_path / "system.wav", tmp_path / "mic.wav")

    deadline = time.time() + 5
    while not events("capture.said") and time.time() < deadline:
        time.sleep(0.01)
    assert [e["line"] for e in events("capture.said")] == ["open System Settings"]
    assert events("capture.refused")[0]["code"] == 3


def test_a_metered_recording_ends_with_a_summary_of_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.config, "CAPTURE_BIN", helper(tmp_path, REPORTS_LEVELS))
    readings: list = []
    recording = capture.start(
        tmp_path / "system.wav", tmp_path / "mic.wav", levels=lambda s, m: readings.append((s, m))
    )
    deadline = time.time() + 5
    while len(readings) < 2 and time.time() < deadline:
        time.sleep(0.01)

    recording.stop()

    (summary,) = events("capture.levels")
    assert summary["metered_s"] == 0.5
    assert summary["mic_silent_s"] == 0.25


# --- the command line ------------------------------------------------------


def test_every_command_leaves_how_it_ended_in_the_log(repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", "stats"])

    cli.main()

    assert [e["command"] for e in events("command.start")] == ["stats"]
    (ended,) = events("command.end")
    assert (ended["command"], ended["outcome"]) == ("stats", "ok")


def test_a_refused_command_is_logged_with_why(repo, monkeypatch):
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", "show", "99"])

    with pytest.raises(SystemExit):
        cli.main()

    (failed,) = events("command.failed")
    assert "no memo with id 99" in failed["error"]
    assert events("command.end")[0]["outcome"] == "failed"


def test_report_prints_where_it_wrote_the_file(repo, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(sys, "argv", ["vtn", "report", "--out", str(tmp_path)])

    cli.main()

    written = Path(capsys.readouterr().out.strip())
    assert written.parent == tmp_path
    assert written.read_text().startswith("# vtn report")
