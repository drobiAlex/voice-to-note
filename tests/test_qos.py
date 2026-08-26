import subprocess

import pytest

from voice_to_note.gateways import GatewayError, qos, sherpa, whisper


def test_a_command_is_wrapped_with_taskpolicy_when_it_is_on_the_path(monkeypatch):
    monkeypatch.setattr(qos.shutil, "which", lambda tool: "/usr/bin/taskpolicy" if tool == "taskpolicy" else None)

    assert qos.background(["whisper-cli", "-m", "model.bin"]) == [
        "taskpolicy", "-c", "utility", "whisper-cli", "-m", "model.bin",
    ]


def test_a_command_falls_back_to_nice_when_taskpolicy_is_absent(monkeypatch):
    monkeypatch.setattr(qos.shutil, "which", lambda tool: "/usr/bin/nice" if tool == "nice" else None)

    assert qos.background(["whisper-cli", "-m", "model.bin"]) == [
        "nice", "-n", "19", "whisper-cli", "-m", "model.bin",
    ]


def test_a_command_is_returned_unchanged_when_neither_tool_is_available(monkeypatch):
    monkeypatch.setattr(qos.shutil, "which", lambda _tool: None)

    assert qos.background(["whisper-cli", "-m", "model.bin"]) == ["whisper-cli", "-m", "model.bin"]


def test_lowering_priority_never_raises_even_when_the_platform_refuses(monkeypatch):
    def refuse(*_args):
        raise OSError("not permitted")

    monkeypatch.setattr(qos.os, "setpriority", refuse)

    qos.lower_priority()


def test_transcribing_runs_the_wrapped_command(tmp_path, monkeypatch):
    bin_path = tmp_path / "whisper-cli"
    bin_path.write_bytes(b"")
    model_path = tmp_path / "model.bin"
    model_path.write_bytes(b"")
    monkeypatch.setattr(whisper.config, "WHISPER_BIN", bin_path)
    monkeypatch.setattr(whisper.config, "WHISPER_MODEL_PATH", model_path)
    monkeypatch.setattr(whisper.config, "VAD_MODEL_PATH", tmp_path / "absent-vad.bin")
    seen = {}

    def background(cmd):
        seen["cmd"] = cmd
        return ["taskpolicy", "-c", "utility", *cmd]

    def run(cmd, **kwargs):
        seen["run_cmd"] = cmd
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(whisper.qos, "background", background)
    monkeypatch.setattr(whisper.subprocess, "run", run)

    with pytest.raises(GatewayError, match="timed out"):
        whisper.transcribe(tmp_path / "memo.wav", 10.0)

    assert seen["run_cmd"] == ["taskpolicy", "-c", "utility", *seen["cmd"]]
    assert str(bin_path) in seen["cmd"]


def test_diarizations_child_process_pool_is_started_at_lowered_priority(monkeypatch):
    seen = {}

    class RecordingPool:
        def __init__(self, *_args, **kwargs):
            seen["kwargs"] = kwargs

        def submit(self, fn, *args):
            class DoneFuture:
                def result(self_inner):
                    return fn(*args)

            return DoneFuture()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(sherpa, "ProcessPoolExecutor", RecordingPool)

    sherpa._isolated(lambda: None)

    assert seen["kwargs"]["initializer"] is qos.lower_priority


def test_the_thread_count_follows_the_performance_cores_when_nobody_has_said(monkeypatch):
    # four threads is whisper's own default whatever machine it is on, which
    # leaves most of a laptop idle for the longest stage in the pipeline
    monkeypatch.setattr(whisper.config, "WHISPER_THREADS", "auto")
    monkeypatch.setattr(whisper.config, "WHISPER_BEAM_SIZE", 5)
    monkeypatch.setattr(qos, "performance_cores", lambda: 8)

    assert whisper.decoding() == ["-t", "8", "-bs", "5"]


def test_a_thread_count_somebody_chose_is_used_as_it_stands(monkeypatch):
    monkeypatch.setattr(whisper.config, "WHISPER_THREADS", "3")
    monkeypatch.setattr(whisper.config, "WHISPER_BEAM_SIZE", 5)

    assert whisper.decoding()[:2] == ["-t", "3"]


def test_a_beam_of_one_brings_best_of_down_with_it(monkeypatch):
    # a beam of one still samples several candidates and picks between them
    # unless best-of comes down too, which is most of what there was to save
    monkeypatch.setattr(whisper.config, "WHISPER_THREADS", "2")
    monkeypatch.setattr(whisper.config, "WHISPER_BEAM_SIZE", 1)

    assert whisper.decoding() == ["-t", "2", "-bs", "1", "-bo", "1"]


def test_a_machine_whose_sysctl_says_nothing_counts_every_core(monkeypatch):
    monkeypatch.setattr(qos.sys, "platform", "darwin")
    monkeypatch.setattr(qos.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(qos.os, "cpu_count", lambda: 6)

    assert qos.performance_cores() == 6
