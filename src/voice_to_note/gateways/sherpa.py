import multiprocessing
import sys
import wave
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

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
from . import GatewayError, qos

SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2
# the onnx models are small enough that more threads stop paying for themselves
MODEL_THREADS = 4
# speech shorter than this is noise, and a pause shorter than this is not a
# speaker change; together they stop one utterance being split into several
MIN_SPEECH_S = 0.3
MIN_SILENCE_S = 0.5

T = TypeVar("T")


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


@contextmanager
def _spawn_safe_stderr() -> Iterator[None]:
    """Starting a child process hands sys.stderr's file descriptor to the
    helper multiprocessing spawns; a full-screen UI replaces stderr with a
    capture object whose fileno() is -1, and the spawn refuses that outright.
    Puts the interpreter's original stream back for the moment processes are
    being started, so there is a real descriptor to hand over."""
    try:
        usable = sys.stderr.fileno() >= 0
    except (AttributeError, OSError, ValueError):
        usable = False
    if usable:
        yield
        return
    captured = sys.stderr
    sys.stderr = sys.__stderr__
    try:
        yield
    finally:
        sys.stderr = captured


def _isolated(fn: Callable[..., T], /, *args: object) -> T:
    """Runs one piece of work in a child process of its own and waits for the
    answer. The wait is on inter-process communication, which leaves the
    calling thread interruptible — the point of paying for a process at all.
    The child is started fresh and shut down again with the call, so nothing
    outlives the work it was made for."""
    # spawn, not fork: fork copies a process that has already loaded onnx
    # runtime threads into a child that cannot safely use them
    ctx = multiprocessing.get_context("spawn")
    with _spawn_safe_stderr():
        pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=qos.lower_priority)
        future = pool.submit(fn, *args)
    with pool:
        return future.result()


def diarize(wav: Path, num_speakers: int | None = None) -> list[Turn]:
    """Works out who spoke when. A caller who already knows how many voices
    are on the recording can pin that count; left unset, the configured
    default is used to guess it instead.

    The work happens in a child process because the diarizer never yields the
    interpreter while it runs — minutes of it on a long memo — and a caller
    that had to sit through that in-process would take the whole app's
    interface down with it, however carefully it had moved off the main
    thread."""
    # the check belongs on this side of the boundary: a user who has not run
    # setup yet should hear so at once, not after waiting for a process to start
    if not config.SEG_MODEL_PATH.exists() or not config.EMB_MODEL_PATH.exists():
        raise GatewayError("diarization models missing — run ./run.sh first")
    try:
        return _isolated(_diarize_here, wav, num_speakers)
    except BrokenProcessPool as e:
        # the child died without saying why — anything it raised would have
        # arrived intact instead
        raise GatewayError(f"diarization crashed — {e}") from e


def _diarize_here(wav: Path, num_speakers: int | None) -> list[Turn]:
    """The diarization itself, wherever it is asked to run. It reads its
    settings from config like everything else: a child process re-imports the
    app and inherits the environment, so it derives the same settings the
    caller would have used."""
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
