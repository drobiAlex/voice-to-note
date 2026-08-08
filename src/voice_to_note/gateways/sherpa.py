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

SAMPLE_RATE = 16000


def load_wav(path: Path) -> np.ndarray:
    """Reads a converted recording into memory for the speaker models."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            raise ValueError(f"{path} is not 16kHz mono")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _embedding_config() -> sherpa_onnx.SpeakerEmbeddingExtractorConfig:
    """The voice-fingerprint model settings, shared by both passes over audio."""
    return sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(config.EMB_MODEL_PATH), num_threads=4
    )


def diarize(wav: Path) -> list[Turn]:
    """Works out who spoke when."""
    if not config.SEG_MODEL_PATH.exists() or not config.EMB_MODEL_PATH.exists():
        raise RuntimeError("diarization models missing — run ./run.sh first")
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(config.SEG_MODEL_PATH)
            ),
            num_threads=4,
        ),
        embedding=_embedding_config(),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=config.NUM_SPEAKERS, threshold=config.DIAR_THRESHOLD
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
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
