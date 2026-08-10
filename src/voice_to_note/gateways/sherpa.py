import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import sherpa_onnx

from .. import config
from ..domain import Turn
from ..transforms.speakers import (
    average_embedding,
    sample_range,
    select_fingerprint_turns,
    turns_from_clusters,
)
from . import GatewayError

SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2
# the onnx models are small enough that more threads stop paying for themselves
MODEL_THREADS = 4
# speech shorter than this is noise, and a pause shorter than this is not a
# speaker change; together they stop one utterance being split into several
MIN_SPEECH_S = 0.3
MIN_SILENCE_S = 0.5


def load_wav(path: Path) -> np.ndarray:
    """Reads a converted recording into memory for the speaker models."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            raise ValueError(f"{path} is not 16kHz mono")
        if w.getsampwidth() != SAMPLE_WIDTH_BYTES:
            # any other width reinterpreted as 16-bit reads as plausible noise
            # rather than failing, and the diarizer would believe it
            raise ValueError(f"{path} is not 16-bit")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _embedding_config() -> sherpa_onnx.SpeakerEmbeddingExtractorConfig:
    """The voice-fingerprint model settings, shared by both passes over audio."""
    return sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(config.EMB_MODEL_PATH), num_threads=MODEL_THREADS
    )


def diarize(wav: Path, num_speakers: int | None = None) -> list[Turn]:
    """Works out who spoke when. A caller who already knows how many voices
    are on the recording can pin that count; left unset, the configured
    default is used to guess it instead."""
    if not config.SEG_MODEL_PATH.exists() or not config.EMB_MODEL_PATH.exists():
        raise GatewayError("diarization models missing — run ./run.sh first")
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(config.SEG_MODEL_PATH)
            ),
            num_threads=MODEL_THREADS,
        ),
        embedding=_embedding_config(),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers is not None else config.NUM_SPEAKERS,
            threshold=config.DIAR_THRESHOLD,
        ),
        min_duration_on=MIN_SPEECH_S,
        min_duration_off=MIN_SILENCE_S,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    result = sd.process(load_wav(wav)).sort_by_start_time()
    return turns_from_clusters((r.start, r.end, r.speaker) for r in result)


def speaker_embeddings(wav: Path, turns: Sequence[Turn]) -> dict[str, np.ndarray]:
    """Takes a voice fingerprint per speaker, so later memos can recognise them."""
    samples = load_wav(wav)
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(_embedding_config())
    by_speaker: dict[str, list[Turn]] = {}
    for t in turns:
        by_speaker.setdefault(t.speaker, []).append(t)
    result: dict[str, np.ndarray] = {}
    for label, ts in by_speaker.items():
        embs = []
        for t in select_fingerprint_turns(ts, len(samples)):
            start, end = sample_range(t)
            embs.append(_embed(ex, samples[start:end]))
        if embs:
            result[label] = average_embedding(embs)
    return result


def _embed(ex: sherpa_onnx.SpeakerEmbeddingExtractor, chunk: np.ndarray) -> np.ndarray:
    """Turns one stretch of speech into the numbers that identify a voice."""
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, chunk)
    stream.input_finished()
    return np.array(ex.compute(stream), dtype=np.float32)
