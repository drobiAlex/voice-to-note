import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import numpy as np

from .. import config
from ..domain import Extraction, Memo, NotesPayload, Segment, Speaker

SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  wav_path TEXT NOT NULL,
  duration_s REAL,
  language TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  project TEXT NOT NULL DEFAULT 'other',
  notes_md TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
  t0_ms INTEGER NOT NULL,
  t1_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  speaker TEXT,
  refined_text TEXT
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

MEMO_COLUMNS = (
    "id, filename, wav_path, duration_s, language, status, created_at, project, updated_at"
)


class Repository:
    """Every SQL statement in the app lives here."""

    def __init__(self, path: Path | str | None = None):
        """Opens the memo database, creating or upgrading it as needed."""
        path = Path(path) if path is not None else config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        # kept so work happening off the main thread can open the same database
        # again: sqlite refuses a connection used from a thread that did not make it
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Brings an older database up to the current shape, a column at a time:
        `embedding` for one made before voice matching, `refined_text` for one
        made before transcript repair, `project` for one made before memos were
        grouped, `notes_md` for one made before notes could be edited, and
        `updated_at` for one made before memos recorded when they last changed."""
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(speakers)")}
        if "embedding" not in cols:
            self.con.execute("ALTER TABLE speakers ADD COLUMN embedding BLOB")
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(segments)")}
        if "refined_text" not in cols:
            self.con.execute("ALTER TABLE segments ADD COLUMN refined_text TEXT")
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(memos)")}
        if "project" not in cols:
            self.con.execute(
                "ALTER TABLE memos ADD COLUMN project TEXT NOT NULL DEFAULT 'other'"
            )
        if "notes_md" not in cols:
            self.con.execute("ALTER TABLE memos ADD COLUMN notes_md TEXT")
        if "updated_at" not in cols:
            self.con.execute("ALTER TABLE memos ADD COLUMN updated_at TEXT")

    def close(self) -> None:
        """Closes the memo database."""
        self.con.close()

    def __enter__(self) -> "Repository":
        """Hands back the already-open database: the connection is made when the
        repository is built, so a block only scopes when it gets closed."""
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        """Closes the database when the command is done, so a long-lived process
        cannot leave connections open behind it."""
        self.close()
        return False

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
        project: str = "other",
    ) -> int:
        """Stores a finished transcription: memo, segments and speakers together."""
        with self.con:
            cur = self.con.execute(
                "INSERT INTO memos (filename, wav_path, duration_s, language, status,"
                " project) VALUES (?,?,?,?,'transcribed',?)",
                (filename, wav_path, duration_s, language, project),
            )
            # sqlite always reports the row it just inserted; the check narrows
            # the type for everything downstream that treats this as an id
            assert cur.lastrowid is not None
            memo_id = cur.lastrowid
            self.con.executemany(
                "INSERT INTO segments (memo_id, t0_ms, t1_ms, text, speaker)"
                " VALUES (?,?,?,?,?)",
                [(memo_id, s.t0_ms, s.t1_ms, s.text, s.speaker) for s in segments],
            )
            self._write_speakers(memo_id, speakers)
        return memo_id

    def memos(self, project: str | None = None, tag: str | None = None) -> list[Memo]:
        """Every memo, newest first, narrowed to a project, to a tag its notes
        carry, or to both at once. Tags are matched whole and whatever case they
        were written in; a memo nobody has extracted has no tags to be found by."""
        sql = f"SELECT {MEMO_COLUMNS} FROM memos"
        where = []
        params: list = []
        if project is not None:
            where.append("project=?")
            params.append(project)
        if tag is not None:
            # a subquery rather than a join: memos and extractions both have an
            # id and a created_at, so joining would mean qualifying every column
            # of the projection, and IN already yields one row per memo
            where.append(
                "id IN (SELECT e.memo_id FROM extractions e"
                " JOIN json_each(e.json, '$.tags') t WHERE lower(t.value)=lower(?))"
            )
            params.append(tag)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [
            _memo(r) for r in self.con.execute(sql + " ORDER BY id DESC", tuple(params))
        ]

    def projects(self) -> list[tuple[str, int]]:
        """Every project that has memos in it and how many, for a sidebar to
        show without a second query per project."""
        return [
            (r["project"], r["n"])
            for r in self.con.execute(
                "SELECT project, count(*) AS n FROM memos GROUP BY project ORDER BY project"
            )
        ]

    def notes_md(self, memo_id: int) -> str | None:
        """A memo's hand-edited notes, if anyone has written any. Kept out of the
        listing projection so drawing a table of memos does not drag every note
        along with it."""
        row = self.con.execute(
            "SELECT notes_md FROM memos WHERE id=?", (memo_id,)
        ).fetchone()
        return row["notes_md"] if row else None

    def _touch(self, memo_id: int) -> None:
        """Marks a memo as changed just now. Every write that alters what a
        reader would see calls this, so `updated_at` means the last change to the
        memo as it reads rather than the last write to any row mentioning it."""
        self.con.execute(
            "UPDATE memos SET updated_at=datetime('now') WHERE id=?", (memo_id,)
        )

    def save_notes_md(self, memo_id: int, markdown: str | None) -> None:
        """Stores a person's own version of a memo's notes, or clears it away
        again with None. The extraction it was written over is left alone."""
        with self.con:
            self.con.execute(
                "UPDATE memos SET notes_md=? WHERE id=?", (markdown, memo_id)
            )
            self._touch(memo_id)

    def set_project(self, memo_id: int, project: str) -> None:
        """Files a memo under a different project."""
        with self.con:
            self.con.execute("UPDATE memos SET project=? WHERE id=?", (project, memo_id))
            self._touch(memo_id)

    def refile_project(self, old: str, new: str) -> int:
        """Files everything under one project into another in a single statement,
        reporting how many memos that carried. Renaming a project and emptying
        one are both this: a project is only a value on its memos, so there is
        nothing else anywhere to rename or delete."""
        with self.con:
            # the stamp _touch makes, written in here rather than called: this is
            # one statement over a whole project by contract, and folding it in
            # keeps the rowcount the count of memos carried
            cur = self.con.execute(
                "UPDATE memos SET project=?, updated_at=datetime('now') WHERE project=?",
                (new, old),
            )
        return cur.rowcount

    def memo(self, memo_id: int) -> Memo | None:
        """One memo's details, or nothing when that id was never stored."""
        row = self.con.execute(
            f"SELECT {MEMO_COLUMNS} FROM memos WHERE id=?", (memo_id,)
        ).fetchone()
        return _memo(row) if row else None

    # --- segments ------------------------------------------------------

    def segments(self, memo_id: int) -> list[Segment]:
        """A memo's transcript in the order it was spoken."""
        return [
            Segment(
                r["t0_ms"], r["t1_ms"], r["text"], r["speaker"], r["id"], r["refined_text"]
            )
            for r in self.con.execute(
                "SELECT id, t0_ms, t1_ms, text, speaker, refined_text FROM segments"
                " WHERE memo_id=? ORDER BY t0_ms",
                (memo_id,),
            )
        ]

    def save_diarization(
        self, memo_id: int, segments: Sequence[Segment], speakers: Sequence[Speaker]
    ) -> None:
        """Records a fresh speaker pass over a memo that already exists."""
        with self.con:
            self.con.executemany(
                "UPDATE segments SET speaker=? WHERE id=?",
                [(s.speaker, s.id) for s in segments],
            )
            self._write_speakers(memo_id, speakers)
            self._touch(memo_id)

    def update_refinements(self, memo_id: int, refinements: Mapping[int, str]) -> None:
        """Records a repair pass over a memo. This becomes the whole set of
        refinements that memo has, so a line the pass left out goes back to the
        words that were actually transcribed."""
        with self.con:
            self.con.execute(
                "UPDATE segments SET refined_text=NULL WHERE memo_id=?", (memo_id,)
            )
            self.con.executemany(
                "UPDATE segments SET refined_text=? WHERE memo_id=? AND id=?",
                [(text, memo_id, seg_id) for seg_id, text in refinements.items()],
            )
            self._touch(memo_id)

    # --- speakers ------------------------------------------------------

    def _write_speakers(self, memo_id: int, speakers: Sequence[Speaker]) -> None:
        """Replaces a memo's speaker roster, voice fingerprints included."""
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
        """What to call each speaker on screen, falling back to their label."""
        return {
            r["label"]: r["name"] or r["label"]
            for r in self.con.execute(
                "SELECT label, name FROM speakers WHERE memo_id=?", (memo_id,)
            )
        }

    def named_speakers(self, memo_id: int) -> dict[str, str]:
        """Only the speakers a person has actually named."""
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
        """Voice fingerprints of everyone named so far, for matching new memos."""
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
        """Names one speaker, reporting whether that label was there to name."""
        with self.con:
            cur = self.con.execute(
                "UPDATE speakers SET name=? WHERE memo_id=? AND label=?",
                (name, memo_id, label),
            )
            # a label that was not there changed nothing anybody reads
            if cur.rowcount:
                self._touch(memo_id)
        return cur.rowcount > 0

    # --- extractions ---------------------------------------------------

    def save_extraction(
        self, memo_id: int, backend: str, data: NotesPayload, *, clear_edited: bool = False
    ) -> None:
        """Files notes against a memo, replacing any earlier attempt. Clearing a
        hand-edited note happens in this same transaction rather than after it:
        a forced re-extraction either lands and takes the edit with it or does
        neither, so an edit can never be left masking notes it was never written
        over. The clearing follows the write on purpose — were it to go first, an
        extraction that then failed would have destroyed somebody's writing."""
        with self.con:
            self.con.execute("DELETE FROM extractions WHERE memo_id=?", (memo_id,))
            self.con.execute(
                "INSERT INTO extractions (memo_id, backend, json) VALUES (?,?,?)",
                (memo_id, backend, json.dumps(data, ensure_ascii=False)),
            )
            self.con.execute("UPDATE memos SET status='extracted' WHERE id=?", (memo_id,))
            if clear_edited:
                self.con.execute("UPDATE memos SET notes_md=NULL WHERE id=?", (memo_id,))
            self._touch(memo_id)

    def extraction(self, memo_id: int) -> Extraction | None:
        """A memo's stored notes, if extraction has run for it."""
        row = self.con.execute(
            "SELECT backend, json, created_at FROM extractions WHERE memo_id=?",
            (memo_id,),
        ).fetchone()
        if not row:
            return None
        # only parse_notes writes here, so the stored JSON already passed its check
        data = cast(NotesPayload, json.loads(row["json"]))
        return Extraction(row["backend"], data, row["created_at"])


def _memo(row: sqlite3.Row) -> Memo:
    """Presents a stored row as a memo."""
    return Memo(
        row["id"],
        row["filename"],
        row["wav_path"],
        row["duration_s"],
        row["language"],
        row["status"],
        row["created_at"],
        row["project"],
        row["updated_at"],
    )
