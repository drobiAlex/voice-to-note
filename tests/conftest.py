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
    said — a chat reply, or the list of models it has pulled."""

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


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
