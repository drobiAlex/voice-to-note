import pytest

from voice_to_note.storage.repository import Repository


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
