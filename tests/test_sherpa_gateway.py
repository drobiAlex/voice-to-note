import wave
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pytest

from voice_to_note.domain import Turn
from voice_to_note.gateways import GatewayError, sherpa

SAMPLES = np.array([0, 16384, -16384, 32767], dtype=np.int16)


def write_wav(path, *, rate=16000, channels=1, sampwidth=2, frames=b"") -> str:
    """Writes a recording with exactly the properties a test wants to hand over."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(frames)
    return str(path)


def test_a_16k_mono_recording_loads_as_samples_the_models_can_read(tmp_path):
    path = write_wav(tmp_path / "memo.wav", frames=SAMPLES.tobytes())

    samples = sherpa.load_wav(path)

    assert samples.dtype == np.float32
    # the speaker models expect amplitudes scaled into -1..1
    assert np.allclose(samples, SAMPLES.astype(np.float32) / 32768.0)


def test_a_recording_at_the_wrong_sample_rate_is_refused(tmp_path):
    path = write_wav(tmp_path / "memo.wav", rate=44100, frames=SAMPLES.tobytes())

    with pytest.raises(ValueError, match="16kHz mono"):
        sherpa.load_wav(path)


def test_a_recording_with_two_channels_is_refused(tmp_path):
    path = write_wav(tmp_path / "memo.wav", channels=2, frames=SAMPLES.tobytes())

    with pytest.raises(ValueError, match="16kHz mono"):
        sherpa.load_wav(path)


def test_a_recording_that_is_not_16_bit_is_refused_rather_than_read_as_noise(tmp_path):
    # right rate and channel count, wrong sample width: reading these bytes as
    # 16-bit gives plausible-looking nonsense, which is worse than a failure
    path = write_wav(tmp_path / "memo.wav", sampwidth=1, frames=bytes([0, 64, 128, 255]))

    with pytest.raises(ValueError, match="16-bit"):
        sherpa.load_wav(path)


# Work handed to a child process is pickled by reference, so anything a test
# sends across the boundary has to be importable by name: module-level
# functions, never closures the test defines inline.
def double(n):
    """Stands in for diarization in the tests that exercise the boundary
    itself rather than what runs on the far side of it."""
    return n * 2


def refuse_as_a_setup_problem():
    """A child failing the way a child with no models to load fails."""
    raise GatewayError("models missing in the child")


def refuse_as_a_bad_recording():
    """A child failing the way a child handed an unreadable recording fails."""
    raise ValueError("not 16kHz mono in the child")


def never_called(*_args):
    """Stands in where a test's point is that the call never happens."""
    raise AssertionError("a child process was started when it should not have been")


@pytest.fixture
def installed_models(tmp_path, monkeypatch):
    """Pretends the diarization models are downloaded, so a test can reach the
    handover the setup check guards."""
    for name in ("SEG_MODEL_PATH", "EMB_MODEL_PATH"):
        path = tmp_path / f"{name}.onnx"
        path.write_bytes(b"")
        monkeypatch.setattr(sherpa.config, name, path)


def test_work_given_to_a_child_process_comes_back_with_its_answer():
    assert sherpa._isolated(double, 21) == 42


def test_a_setup_problem_hit_inside_a_child_reaches_the_caller_with_its_advice():
    with pytest.raises(GatewayError, match="models missing in the child"):
        sherpa._isolated(refuse_as_a_setup_problem)


def test_a_bad_recording_found_inside_a_child_reaches_the_caller_with_its_reason():
    with pytest.raises(ValueError, match="not 16kHz mono in the child"):
        sherpa._isolated(refuse_as_a_bad_recording)


def test_missing_models_are_refused_before_a_child_process_is_paid_for(tmp_path, monkeypatch):
    monkeypatch.setattr(sherpa.config, "SEG_MODEL_PATH", tmp_path / "absent.onnx")
    monkeypatch.setattr(sherpa, "_isolated", never_called)

    with pytest.raises(GatewayError, match="models missing"):
        sherpa.diarize(tmp_path / "memo.wav")


def test_diarizing_hands_the_recording_and_a_pinned_speaker_count_to_a_child(
    installed_models, tmp_path, monkeypatch
):
    turns = [Turn(0, 1000, "S1")]
    calls = []

    def isolated(fn, *args):
        calls.append((fn, args))
        return turns

    monkeypatch.setattr(sherpa, "_isolated", isolated)
    wav = tmp_path / "memo.wav"

    assert sherpa.diarize(wav, 3) == turns
    assert calls == [(sherpa._diarize_here, (wav, 3))]


def test_diarizing_without_a_speaker_count_leaves_the_child_to_use_the_configured_one(
    installed_models, tmp_path, monkeypatch
):
    calls = []

    def isolated(fn, *args):
        calls.append(args)
        return []

    monkeypatch.setattr(sherpa, "_isolated", isolated)
    wav = tmp_path / "memo.wav"

    sherpa.diarize(wav)

    assert calls == [(wav, None)]


def test_a_child_that_dies_outright_is_reported_as_a_failed_diarization(
    installed_models, tmp_path, monkeypatch
):
    def isolated(fn, *args):
        raise BrokenProcessPool("terminated abruptly")

    monkeypatch.setattr(sherpa, "_isolated", isolated)

    with pytest.raises(GatewayError, match="diarization crashed — terminated abruptly"):
        sherpa.diarize(tmp_path / "memo.wav")
