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


def speaker_embeddings(wav: Path, turns: list[dict]) -> dict[str, np.ndarray]:
    # voice fingerprint per speaker: average embeddings of their longest
    # turns, up to ~30s of audio each, L2-normalized for cosine matching
    samples = load_wav(wav)
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(config.EMB_MODEL_PATH), num_threads=4
        )
    )
    by_speaker: dict[str, list[dict]] = {}
    for t in turns:
        by_speaker.setdefault(t["speaker"], []).append(t)
    result: dict[str, np.ndarray] = {}
    for label, ts in by_speaker.items():
        ts = sorted(ts, key=lambda t: t["end_ms"] - t["start_ms"], reverse=True)
        embs, used_ms = [], 0
        for t in ts:
            if used_ms >= 30_000:
                break
            chunk = samples[t["start_ms"] * 16 : t["end_ms"] * 16]
            if len(chunk) < 8000:
                continue
            stream = ex.create_stream()
            stream.accept_waveform(16000, chunk)
            stream.input_finished()
            embs.append(np.array(ex.compute(stream), dtype=np.float32))
            used_ms += t["end_ms"] - t["start_ms"]
        if embs:
            v = np.mean(embs, axis=0)
            result[label] = v / (np.linalg.norm(v) or 1.0)
    return result


def match_known_speakers(con, embs: dict[str, np.ndarray]) -> dict[str, tuple[str, float]]:
    # compare against named speakers from other memos; best cosine wins
    pool: dict[str, list[np.ndarray]] = {}
    for r in con.execute(
        "SELECT name, embedding FROM speakers"
        " WHERE name IS NOT NULL AND embedding IS NOT NULL"
    ):
        pool.setdefault(r["name"], []).append(
            np.frombuffer(r["embedding"], dtype=np.float32)
        )
    if not pool:
        return {}
    centroids = {}
    for name, vs in pool.items():
        v = np.mean(vs, axis=0)
        centroids[name] = v / (np.linalg.norm(v) or 1.0)
    matches = {}
    for label, v in embs.items():
        name, sim = max(
            ((n, float(np.dot(v, c))) for n, c in centroids.items()),
            key=lambda x: x[1],
        )
        if sim >= config.MATCH_THRESHOLD:
            matches[label] = (name, sim)
    return matches


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
