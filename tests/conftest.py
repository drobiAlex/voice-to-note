import pytest

from voice_to_note.domain import Segment
from voice_to_note.storage.repository import Repository


def seg(i: int, text: str | None = None, speaker: str = "S1") -> Segment:
    """One stored segment, identified the way the database identifies it.
    Passing text="" means an empty line, not a default one."""
    return Segment(i * 1000, i * 1000 + 900, f"line {i}" if text is None else text, speaker, i)


def transcript(n: int) -> list[Segment]:
    """A stored transcript of n lines, in the order they were spoken."""
    return [seg(i) for i in range(n)]


class FakeResponse:
    """An HTTP 200 whose body is whatever the test wants the server to have
    said — a chat reply, the list of models it has pulled, or a download's
    bytes. A length and a chunk size let a download test see the same
    multi-read progress a live server delivering a large file would give."""

    def __init__(self, body: bytes, length: int | None = None, chunk_size: int | None = None):
        self.body = body
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self.chunk_size = chunk_size

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        n = self.chunk_size if self.chunk_size is not None else size
        if n < 0:
            chunk, self.body = self.body, b""
        else:
            chunk, self.body = self.body[:n], self.body[n:]
        return chunk


class StubRepo:
    """Stands in for the database where a test only cares about what got called."""

    def __enter__(self) -> "StubRepo":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def repo(tmp_path):
    """A memo database of a test's own, closed again when the test ends."""
    r = Repository(tmp_path / "memos.db")
    yield r
    r.close()
