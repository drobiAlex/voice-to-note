import wave

import numpy as np
import pytest

from voice_to_note.gateways import sherpa

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
