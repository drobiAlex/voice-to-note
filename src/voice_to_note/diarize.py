import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

from . import config


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError(f"{path} is not 16kHz mono")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def diarize(wav: Path) -> list[dict]:
    if not config.SEG_MODEL_PATH.exists() or not config.EMB_MODEL_PATH.exists():
        raise RuntimeError("diarization models missing — run ./run.sh first")
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(config.SEG_MODEL_PATH)
            ),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(config.EMB_MODEL_PATH), num_threads=4
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=config.NUM_SPEAKERS, threshold=config.DIAR_THRESHOLD
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    result = sd.process(load_wav(wav)).sort_by_start_time()
    order: dict[int, int] = {}
    turns = []
    for r in result:
        if r.speaker not in order:
            order[r.speaker] = len(order) + 1
        turns.append(
            {
                "start_ms": int(r.start * 1000),
                "end_ms": int(r.end * 1000),
                "speaker": f"S{order[r.speaker]}",
            }
        )
    return turns


def assign_speakers(segs: list[dict], turns: list[dict]) -> None:
    # max-overlap wins; segments in gaps fall back to the nearest turn boundary
    for s in segs:
        best, best_ov = None, 0
        for t in turns:
            ov = min(s["t1_ms"], t["end_ms"]) - max(s["t0_ms"], t["start_ms"])
            if ov > best_ov:
                best, best_ov = t["speaker"], ov
        if best is None and turns:
            mid = (s["t0_ms"] + s["t1_ms"]) // 2
            nearest = min(
                turns,
                key=lambda t: min(abs(mid - t["start_ms"]), abs(mid - t["end_ms"])),
            )
            best = nearest["speaker"]
        s["speaker"] = best
