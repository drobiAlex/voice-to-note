import fcntl
import json
import os
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import config

# the live file and the ones rotated out behind it. The size cap is split
# evenly between them, so the log never takes more than log_max_mb however the
# writes fall, and the newest events are always the ones kept
FILES_KEPT = 3

# the longest any one value is written at. An error's message can carry a whole
# tool's stderr, and one line has to stay a line a person can read — and must
# not, on its own, be what pushes a rotation through and loses everything older
FIELD_MAX_CHARS = 2000

LIVE_NAME = "events.jsonl"


def files() -> list[Path]:
    """Every file the log is spread over, oldest first: the rotated ones by
    their number, highest first, then the one being written."""
    rotated = [config.LOGS_DIR / f"events.{n}.jsonl" for n in range(FILES_KEPT - 1, 0, -1)]
    return [*rotated, config.LOGS_DIR / LIVE_NAME]


def record(event: str, **fields: Any) -> None:
    """Appends one event, timestamped, as a line of JSON. Never raises: the log
    is there to explain what went wrong, and a full disk or a read-only home
    folder must not be what takes a recording or a pipeline down instead.

    Written under a lock on a file beside the log, because the menu bar's own
    `vtn record`, a TUI and a terminal may all be writing at once, and checking
    the size, rotating and appending have to happen as one step or two writers
    rotate the same file twice and lose a third of the history between them."""
    if config.LOG_MAX_MB <= 0:
        return
    line = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        "event": event,
        **{key: _bounded(value) for key, value in fields.items()},
    }
    data = (json.dumps(line, ensure_ascii=False, default=str) + "\n").encode()
    try:
        _append(data, int(config.LOG_MAX_MB * 1024 * 1024 / FILES_KEPT))
    except OSError:
        pass


def _bounded(value: Any) -> Any:
    """A value short enough to keep one event a readable line."""
    if isinstance(value, str) and len(value) > FIELD_MAX_CHARS:
        return value[:FIELD_MAX_CHARS] + f"… ({len(value) - FIELD_MAX_CHARS} more chars)"
    return value


def _append(data: bytes, per_file: int) -> None:
    """Writes one line, rotating first when it would carry the live file past
    its share of the cap. A file already empty is never rotated, so a line
    larger than the whole share still lands rather than spinning."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOGS_DIR / "events.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        oldest_first = files()
        live = oldest_first[-1]
        size = live.stat().st_size if live.exists() else 0
        if size and size + len(data) > per_file:
            # each file moves one place older, and the oldest falls off the end
            for older, newer in zip(oldest_first, oldest_first[1:], strict=False):
                if newer.exists():
                    newer.replace(older)
        with open(live, "ab") as out:
            out.write(data)


def tail(limit: int) -> list[str]:
    """The newest lines of the log, oldest of them first, across every file it
    is spread over. Each file is capped, so reading them through is a few
    megabytes at most, and a deque keeps only what is asked for."""
    kept: deque[str] = deque(maxlen=limit)
    for path in files():
        try:
            with open(path, encoding="utf-8", errors="replace") as lines:
                kept.extend(line.rstrip("\n") for line in lines)
        except FileNotFoundError:
            continue
    return list(kept)


def size_bytes() -> int:
    """What the log is taking on disk right now, every file of it."""
    return sum(path.stat().st_size for path in files() if path.exists())


def text_tail(path: Path, max_bytes: int) -> str:
    """The end of some other log, a size-capped slice from the last newline in
    reach, so a log nobody trims cannot make a report as large as itself."""
    try:
        with open(path, "rb") as raw:
            raw.seek(0, os.SEEK_END)
            end = raw.tell()
            raw.seek(max(0, end - max_bytes))
            chunk = raw.read()
    except FileNotFoundError:
        return ""
    text = chunk.decode("utf-8", errors="replace")
    if len(chunk) == max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text
