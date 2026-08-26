import struct
import subprocess
import wave

import numpy as np
import pytest

from voice_to_note import cli, config, services
from voice_to_note.domain import Segment, TrackFormat
from voice_to_note.gateways import GatewayError, audio, capture, whisper
from voice_to_note.storage.repository import Repository
from voice_to_note.transforms.live import cut_offset, mono, prompt_tail, shifted

RATE = 16000
INT16 = TrackFormat(rate=RATE, channels=1, bits=16, is_float=False)


def tone(seconds: float, rate: int = RATE, level: float = 0.4) -> np.ndarray:
    """A stretch of something being said, loudly enough to be speech."""
    t = np.arange(int(seconds * rate)) / rate
    return (level * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def hush(seconds: float, rate: int = RATE) -> np.ndarray:
    """A stretch of nobody talking."""
    return np.zeros(int(seconds * rate), dtype=np.float32)


def write_wav(path, samples: np.ndarray, rate: int = RATE, channels: int = 1) -> None:
    """A recorded track on disk, in the shape the recorder writes one."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())


def append_frames(path, samples: np.ndarray) -> None:
    """More of the meeting, written the way the helper writes it: onto the end
    of a file whose header still describes the length it had when it opened."""
    with path.open("ab") as f:
        f.write((samples * 32767).astype(np.int16).tobytes())


# --- reading a track that is still being written ---------------------------


def test_a_track_is_read_by_what_is_on_disk_not_by_what_its_header_claims(tmp_path):
    # the recorder writes the length short and corrects it only on close, so a
    # reader believing the header would find every meeting empty
    track = tmp_path / "system.wav"
    write_wav(track, hush(0))
    append_frames(track, tone(2.0))

    reader = capture.TrackReader(track)

    assert reader.available() == 2 * RATE


def test_a_reader_takes_only_what_it_has_been_told_to_keep(tmp_path):
    track = tmp_path / "system.wav"
    write_wav(track, tone(3.0))
    reader = capture.TrackReader(track)

    looked = reader.peek(RATE)
    reader.advance(RATE // 2)

    assert len(looked) == RATE * 2
    assert reader.taken_s == 0.5
    assert reader.available() == int(2.5 * RATE)


def test_a_partly_written_frame_is_not_offered_until_it_is_whole(tmp_path):
    track = tmp_path / "system.wav"
    write_wav(track, tone(1.0), channels=1)
    with track.open("ab") as f:
        f.write(b"\x01")

    assert capture.TrackReader(track).available() == RATE


def test_a_track_that_is_not_a_recording_says_so_rather_than_reading_as_noise(tmp_path):
    track = tmp_path / "system.wav"
    track.write_bytes(b"this is not audio at all")

    with pytest.raises(GatewayError, match="not a wav recording"):
        capture.TrackReader(track)


def test_a_float_track_is_recognised_through_the_extensible_wrapper(tmp_path):
    # capture.swift records in whatever the device offers, and 32-bit float
    # read as integers transcribes as noise
    track = tmp_path / "system.wav"
    body = b"\x00" * 16
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 48000, 48000 * 4, 4, 32) + struct.pack(
        "<HHIH", 22, 32, 0, 3
    )
    track.write_bytes(
        b"RIFF" + b"\x00" * 4 + b"WAVE"
        + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
        + b"data" + len(body).to_bytes(4, "little") + body
    )

    assert capture.TrackReader(track).format == TrackFormat(48000, 1, 32, True)


# --- deciding where to cut -------------------------------------------------


def test_a_chunk_is_cut_in_the_pause_rather_than_through_a_word():
    # measured: cutting on the minute regardless of what is being said costs
    # 160% more decoding than the same audio transcribed whole
    samples = np.concatenate([tone(9.0), hush(1.0), tone(9.0)])

    at = cut_offset([(samples, RATE)], target_s=7.0, search_s=4.0)

    assert 9.0 <= at <= 10.0


def test_a_cut_is_placed_where_neither_side_of_the_meeting_is_talking():
    # one side pausing is not a pause: the other is mid-sentence
    system = np.concatenate([tone(4.0), hush(4.0), tone(4.0)])
    microphone = np.concatenate([hush(4.0), tone(4.0), hush(4.0)])

    at = cut_offset([(system, RATE), (microphone, RATE)], target_s=6.0, search_s=4.0)

    assert at <= 4.5 or at >= 7.5


def test_a_stretch_too_short_to_search_is_cut_where_it_was_asked_to_be():
    assert cut_offset([(tone(0.1), RATE)], target_s=60.0) == 60.0
    assert cut_offset([], target_s=60.0) == 60.0


def test_the_two_channels_of_a_track_are_averaged_rather_than_one_of_them_taken():
    stereo = np.zeros(200, dtype=np.float32)
    stereo[1::2] = 0.5  # everything on the right channel

    folded = mono(
        (stereo * 32767).astype(np.int16).tobytes(),
        TrackFormat(RATE, channels=2, bits=16, is_float=False),
    )

    assert len(folded) == 100
    assert folded.mean() > 0.2


def test_a_sample_width_nothing_can_read_is_refused_rather_than_reinterpreted():
    with pytest.raises(ValueError, match="unsupported sample width"):
        mono(b"\x00" * 12, TrackFormat(RATE, channels=1, bits=24, is_float=False))


# --- putting the chunks back together --------------------------------------


def test_a_chunks_lines_are_timed_from_the_meeting_not_from_the_chunk():
    lines = [Segment(0, 900, "hello"), Segment(1000, 1900, "again")]

    assert [s.t0_ms for s in shifted(lines, 180_000)] == [180_000, 181_000]


def test_the_words_handed_to_the_next_chunk_are_the_last_ones_spoken():
    lines = [Segment(0, 1, " one two "), Segment(1, 2, "three four")]

    assert prompt_tail(lines, words=3) == "two three four"
    assert prompt_tail([], words=3) == ""


# --- mixing one stretch of both tracks -------------------------------------


def test_a_stretch_of_both_tracks_is_mixed_the_way_the_whole_meeting_is(tmp_path, monkeypatch):
    # a chunk transcribed live and the archive transcribed afterwards have to
    # be the same audio, or re-running a memo would change its transcript
    seen: list[list[str]] = []

    def record(cmd, **_kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(audio.subprocess, "run", record)

    audio.mix_chunk(
        [(b"\x00" * 64, TrackFormat(48000, 2, 32, True)), (b"\x00" * 32, INT16)],
        tmp_path / "chunk.wav",
    )

    cmd = seen[0]
    assert "f32le" in cmd and "s16le" in cmd
    assert "amix=inputs=2:duration=longest" in cmd
    assert cmd[cmd.index("-ar", cmd.index("-filter_complex")) + 1] == "16000"


def test_a_stretch_with_only_one_side_recorded_is_mixed_without_an_amix(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        audio.subprocess,
        "run",
        lambda cmd, **_k: (seen.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    audio.mix_chunk([(b"", INT16), (b"\x00" * 32, INT16)], tmp_path / "chunk.wav")

    assert "-filter_complex" not in seen[0]


def test_a_stretch_with_no_audio_in_it_at_all_is_refused(tmp_path):
    with pytest.raises(GatewayError, match="no audio"):
        audio.mix_chunk([(b"", INT16)], tmp_path / "chunk.wav")


# --- a meeting transcribed while it is recorded ----------------------------


class FakeWhisper:
    """The transcriber, without the model: every chunk comes back as one line
    naming how long it was, and every prompt it was handed is remembered."""

    def __init__(self):
        self.prompts: list[str] = []
        self.durations: list[float] = []

    def __call__(self, wav, duration_s, model=None, prompt=""):
        self.prompts.append(prompt)
        self.durations.append(duration_s)
        n = len(self.durations)
        return {
            "result": {"language": "en"},
            "transcription": [
                {"offsets": {"from": 0, "to": 500}, "text": f" chunk {n} words"}
            ],
        }


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A meeting being recorded, with the models stood in for: two tracks on
    disk, a transcriber that answers at once, and a database to store into."""
    monkeypatch.setattr(config, "LIVE_TRANSCRIBE", "on")
    monkeypatch.setattr(config, "LIVE_CHUNK_S", 4)
    monkeypatch.setattr(config, "LIVE_MODEL", "")
    monkeypatch.setattr(whisper, "require", lambda model=None: None)
    fake = FakeWhisper()
    monkeypatch.setattr(whisper, "transcribe", fake)
    monkeypatch.setattr(audio, "mix_chunk", lambda parts, dst: dst.write_bytes(b"wav"))
    monkeypatch.setattr(services, "LIVE_POLL_S", 0.01)
    system, mic = tmp_path / "system.wav", tmp_path / "mic.wav"
    write_wav(system, hush(0))
    write_wav(mic, hush(0))
    return system, mic, fake


def spoken(seconds: float) -> np.ndarray:
    """Someone talking, with a pause in the middle for a cut to land in."""
    return np.concatenate([tone(seconds / 2), hush(0.6), tone(seconds / 2)])


def test_a_meeting_is_stored_a_stretch_at_a_time_while_it_is_still_being_recorded(
    tmp_path, live
):
    system, mic, _fake = live
    with Repository(tmp_path / "db.sqlite") as repo:
        session = services.start_live(repo, system, mic, "meeting.m4a", "work")
        assert session is not None
        append_frames(system, spoken(20.0))
        append_frames(mic, spoken(20.0))
        result = session.stop()

        stored = repo.segments(session.memo_id)

    assert result.failure is None
    assert result.language == "en"
    assert len(stored) == result.segment_count > 1
    # every stretch is timed from the start of the meeting, and they follow on
    assert stored == sorted(stored, key=lambda s: s.t0_ms)
    assert stored[-1].t0_ms > 0


def test_the_last_stretch_of_a_meeting_is_read_when_the_recording_stops(tmp_path, live):
    # shorter than a chunk, so nothing but stopping would ever transcribe it
    system, mic, _fake = live
    with Repository(tmp_path / "db.sqlite") as repo:
        session = services.start_live(repo, system, mic, "meeting.m4a", "work")
        assert session is not None
        append_frames(system, spoken(2.0))
        append_frames(mic, spoken(2.0))

        result = session.stop()

    assert result.segment_count == 1


def test_each_stretch_is_told_what_the_one_before_it_ended_with(tmp_path, live):
    system, mic, fake = live
    with Repository(tmp_path / "db.sqlite") as repo:
        session = services.start_live(repo, system, mic, "meeting.m4a", "work")
        assert session is not None
        append_frames(system, spoken(30.0))
        append_frames(mic, spoken(30.0))
        session.stop()

    assert fake.prompts[0] == ""
    assert fake.prompts[1].endswith("chunk 1 words")


def test_a_stretch_that_fails_costs_the_live_transcript_and_not_the_recording(
    tmp_path, live, monkeypatch
):
    system, mic, _fake = live

    def refuse(*_a, **_k):
        raise GatewayError("whisper-cli failed")

    monkeypatch.setattr(whisper, "transcribe", refuse)
    with Repository(tmp_path / "db.sqlite") as repo:
        session = services.start_live(repo, system, mic, "meeting.m4a", "work")
        assert session is not None
        append_frames(system, spoken(20.0))
        append_frames(mic, spoken(20.0))
        result = session.stop()

    assert result.segment_count == 0
    assert result.failure is not None
    assert system.exists() and mic.exists()


def test_transcribing_as_it_records_is_skipped_rather_than_failed_when_it_cannot_run(
    tmp_path, live, monkeypatch
):
    system, mic, _fake = live
    monkeypatch.setattr(config, "LIVE_MODEL", "tiny")
    said: list[str] = []

    with Repository(tmp_path / "db.sqlite") as repo:
        monkeypatch.setattr(
            whisper, "require", lambda model=None: (_ for _ in ()).throw(GatewayError("no model"))
        )
        assert services.start_live(repo, system, mic, "m.m4a", "work", log=said.append) is None
        assert repo.memos() == []

    assert "no model" in said[0]


def test_a_meeting_nobody_asked_to_transcribe_live_opens_no_memo(tmp_path, live, monkeypatch):
    system, mic, _fake = live
    monkeypatch.setattr(config, "LIVE_TRANSCRIBE", "off")

    with Repository(tmp_path / "db.sqlite") as repo:
        assert services.start_live(repo, system, mic, "m.m4a", "work") is None


def test_a_live_memo_is_finished_by_the_archive_rather_than_re_transcribed(
    tmp_path, live, monkeypatch
):
    system, mic, fake = live
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        audio, "to_wav16k", lambda src, dst: (dst.parent.mkdir(parents=True, exist_ok=True), dst.write_bytes(b"wav"))
    )
    monkeypatch.setattr(audio, "duration_seconds", lambda path: 90.0)
    monkeypatch.setattr(audio, "recorded_at", lambda path: "2026-08-26T09:00:00Z")

    with Repository(tmp_path / "db.sqlite") as repo:
        session = services.start_live(repo, system, mic, "meeting.m4a", "work")
        assert session is not None
        append_frames(system, spoken(20.0))
        append_frames(mic, spoken(20.0))
        heard = session.stop()

        result = services.finish_live(
            repo, session.memo_id, tmp_path / "meeting.m4a",
            language=heard.language, diarize=False,
        )
        memo = repo.memo(session.memo_id)

    assert memo is not None
    assert memo.status == "transcribed"
    assert memo.duration_s == 90.0
    assert memo.recorded_at == "2026-08-26T09:00:00Z"
    assert result.segment_count == heard.segment_count
    # the meeting was transcribed once, while it was happening
    assert len(fake.durations) == result.segment_count


# --- what `vtn record` does with what it heard ------------------------------


class Taping:
    """A meeting being taped, standing in for the helper: the audio arrives
    while the caller is sitting through the recording, exactly as it does when
    somebody is in a real meeting."""

    def __init__(self, system, mic, seconds: float = 20.0):
        self._tracks = (system, mic)
        self._seconds = seconds

    def wait(self) -> int:
        for track in self._tracks:
            append_frames(track, spoken(self._seconds))
        return 0

    def stop(self) -> None:
        pass


@pytest.fixture
def recorded(tmp_path, monkeypatch, live):
    """`vtn record` with everything outside the process stood in for: a helper
    that tapes, an ffmpeg that merges, and a database in a temporary file."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(
        capture, "start",
        lambda system, mic, **_k: (
            system.parent.mkdir(parents=True, exist_ok=True),
            write_wav(system, hush(0)),
            write_wav(mic, hush(0)),
            Taping(system, mic),
        )[3],
    )
    monkeypatch.setattr(audio, "merge_tracks", lambda s, m, dst: dst.write_bytes(b"m4a"))
    monkeypatch.setattr(
        audio, "to_wav16k",
        lambda src, dst: (dst.parent.mkdir(parents=True, exist_ok=True), dst.write_bytes(b"wav"))[0],
    )
    monkeypatch.setattr(audio, "duration_seconds", lambda path: 20.0)
    monkeypatch.setattr(audio, "recorded_at", lambda path: "2026-08-26T09:00:00Z")

    def record(db):
        monkeypatch.setattr(cli, "Repository", lambda *a, **k: Repository(db))
        monkeypatch.setattr(cli.sys, "argv", ["vtn", "record", "--steps", ""])
        cli.main()

    return record


def test_a_taped_meeting_is_already_transcribed_by_the_time_the_recording_stops(
    tmp_path, recorded, monkeypatch
):
    # the whole point: no second pass over the recording once the meeting ends
    def never(*_a, **_k):
        raise AssertionError("the meeting was transcribed all over again")

    monkeypatch.setattr(services, "process_memo", never)

    recorded(tmp_path / "db.sqlite")

    with Repository(tmp_path / "db.sqlite") as repo:
        memos = repo.memos()
        assert len(memos) == 1
        assert memos[0].status == "transcribed"
        assert repo.segments(memos[0].id)


def test_a_meeting_the_live_pass_could_not_read_is_transcribed_the_ordinary_way(
    tmp_path, recorded, monkeypatch
):
    monkeypatch.setattr(
        whisper, "transcribe",
        lambda *_a, **_k: (_ for _ in ()).throw(GatewayError("whisper-cli failed")),
    )
    ordinary: list[str] = []

    def process(repo, src, **kwargs):
        ordinary.append(src.name)
        return services.ProcessResult(repo.start_memo(filename=src.name, project="other"), 0, [], "")

    monkeypatch.setattr(services, "process_memo", process)

    recorded(tmp_path / "db.sqlite")

    assert ordinary and ordinary[0].startswith("meeting-")
    with Repository(tmp_path / "db.sqlite") as repo:
        # the memo opened for the live pass is gone, not left beside the real one
        assert len(repo.memos()) == 1
