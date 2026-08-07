import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from .. import config
from ..domain import Extraction, Memo, Segment, Speaker

SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  wav_path TEXT NOT NULL,
  duration_s REAL,
  language TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
  t0_ms INTEGER NOT NULL,
  t1_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  speaker TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
  id INTEGER PRIMARY KEY,
  memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
  backend TEXT NOT NULL,
  json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS speakers (
  id INTEGER PRIMARY KEY,
  memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  name TEXT,
  UNIQUE(memo_id, label)
);
"""

MEMO_COLUMNS = "id, filename, wav_path, duration_s, language, status, created_at"


class Repository:
    """Every SQL statement in the app lives here."""

    def __init__(self, path: Path | str | None = None):
        path = Path(path) if path is not None else config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(speakers)")}
        if "embedding" not in cols:
            self.con.execute("ALTER TABLE speakers ADD COLUMN embedding BLOB")

    def close(self) -> None:
        self.con.close()

    # --- memos ---------------------------------------------------------

    def create_memo(
        self,
        *,
        filename: str,
        wav_path: str,
        duration_s: float,
        language: str,
        segments: Sequence[Segment],
        speakers: Sequence[Speaker] = (),
    ) -> int:
        with self.con:
            cur = self.con.execute(
                "INSERT INTO memos (filename, wav_path, duration_s, language, status)"
                " VALUES (?,?,?,?,'transcribed')",
                (filename, wav_path, duration_s, language),
            )
            memo_id = cur.lastrowid
            self.con.executemany(
                "INSERT INTO segments (memo_id, t0_ms, t1_ms, text, speaker)"
                " VALUES (?,?,?,?,?)",
                [(memo_id, s.t0_ms, s.t1_ms, s.text, s.speaker) for s in segments],
            )
            self._write_speakers(memo_id, speakers)
        return memo_id

    def memos(self) -> list[Memo]:
        return [
            _memo(r)
            for r in self.con.execute(f"SELECT {MEMO_COLUMNS} FROM memos ORDER BY id DESC")
        ]

    def memo(self, memo_id: int) -> Memo | None:
        row = self.con.execute(
            f"SELECT {MEMO_COLUMNS} FROM memos WHERE id=?", (memo_id,)
        ).fetchone()
        return _memo(row) if row else None

    # --- segments ------------------------------------------------------

    def segments(self, memo_id: int) -> list[Segment]:
        return [
            Segment(r["t0_ms"], r["t1_ms"], r["text"], r["speaker"], r["id"])
            for r in self.con.execute(
                "SELECT id, t0_ms, t1_ms, text, speaker FROM segments"
                " WHERE memo_id=? ORDER BY t0_ms",
                (memo_id,),
            )
        ]

    def save_diarization(
        self, memo_id: int, segments: Sequence[Segment], speakers: Sequence[Speaker]
    ) -> None:
        with self.con:
            self.con.executemany(
                "UPDATE segments SET speaker=? WHERE id=?",
                [(s.speaker, s.id) for s in segments],
            )
            self._write_speakers(memo_id, speakers)

    # --- speakers ------------------------------------------------------

    def _write_speakers(self, memo_id: int, speakers: Sequence[Speaker]) -> None:
        self.con.execute("DELETE FROM speakers WHERE memo_id=?", (memo_id,))
        self.con.executemany(
            "INSERT INTO speakers (memo_id, label, name, embedding) VALUES (?,?,?,?)",
            [
                (
                    memo_id,
                    s.label,
                    s.name,
                    s.embedding.tobytes() if s.embedding is not None else None,
                )
                for s in speakers
            ],
        )

    def display_names(self, memo_id: int) -> dict[str, str]:
        return {
            r["label"]: r["name"] or r["label"]
            for r in self.con.execute(
                "SELECT label, name FROM speakers WHERE memo_id=?", (memo_id,)
            )
        }

    def named_speakers(self, memo_id: int) -> dict[str, str]:
        return {
            r["label"]: r["name"]
            for r in self.con.execute(
                "SELECT label, name FROM speakers WHERE memo_id=? AND name IS NOT NULL",
                (memo_id,),
            )
        }

    def known_embeddings(
        self, exclude_memo_id: int | None = None
    ) -> dict[str, list[np.ndarray]]:
        sql = (
            "SELECT name, embedding FROM speakers"
            " WHERE name IS NOT NULL AND embedding IS NOT NULL"
        )
        params: tuple = ()
        if exclude_memo_id is not None:
            sql += " AND memo_id != ?"
            params = (exclude_memo_id,)
        pool: dict[str, list[np.ndarray]] = {}
        for r in self.con.execute(sql, params):
            pool.setdefault(r["name"], []).append(
                np.frombuffer(r["embedding"], dtype=np.float32)
            )
        return pool

    def rename_speaker(self, memo_id: int, label: str, name: str) -> bool:
        with self.con:
            cur = self.con.execute(
                "UPDATE speakers SET name=? WHERE memo_id=? AND label=?",
                (name, memo_id, label),
            )
        return cur.rowcount > 0

    # --- extractions ---------------------------------------------------

    def save_extraction(self, memo_id: int, backend: str, data: dict) -> None:
        with self.con:
            self.con.execute("DELETE FROM extractions WHERE memo_id=?", (memo_id,))
            self.con.execute(
                "INSERT INTO extractions (memo_id, backend, json) VALUES (?,?,?)",
                (memo_id, backend, json.dumps(data, ensure_ascii=False)),
            )
            self.con.execute("UPDATE memos SET status='extracted' WHERE id=?", (memo_id,))

    def extraction(self, memo_id: int) -> Extraction | None:
        row = self.con.execute(
            "SELECT backend, json, created_at FROM extractions WHERE memo_id=?",
            (memo_id,),
        ).fetchone()
        if not row:
            return None
        return Extraction(row["backend"], json.loads(row["json"]), row["created_at"])


def _memo(row: sqlite3.Row) -> Memo:
    return Memo(
        row["id"],
        row["filename"],
        row["wav_path"],
        row["duration_s"],
        row["language"],
        row["status"],
        row["created_at"],
    )
