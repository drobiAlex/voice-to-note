import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import numpy as np

from .. import config
from ..domain import (
    ActionItem,
    Extraction,
    Memo,
    MemoListing,
    NotesPayload,
    Segment,
    Speaker,
    Todo,
)
from ..transforms.todos import StoredTodo, reconcile, todo_items

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
  updated_at TEXT,
  recorded_at TEXT,
  archived_at TEXT
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
CREATE TABLE IF NOT EXISTS todos (
  id INTEGER PRIMARY KEY,
  memo_id INTEGER NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  norm TEXT NOT NULL,
  owner TEXT NOT NULL DEFAULT '',
  deadline TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
  done_at TEXT,
  touched INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

MEMO_COLUMNS = (
    "id, filename, wav_path, duration_s, language, status, created_at, project,"
    " updated_at, recorded_at, archived_at"
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
        # asked before the schema script runs, since that script is what creates
        # the table: a database written before to-dos were tracked has every
        # extraction it already holds still to be taken in
        first_todos = not self._table_exists("todos")
        self.con.executescript(SCHEMA)
        self._migrate()
        if first_todos:
            self._backfill_todos()

    def _table_exists(self, name: str) -> bool:
        """Whether this database already carries a table, for a migration that
        adds a whole one rather than a column."""
        return (
            self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _migrate(self) -> None:
        """Brings an older database up to the current shape, a column at a time:
        `embedding` for one made before voice matching, `refined_text` for one
        made before transcript repair, `project` for one made before memos were
        grouped, `notes_md` for one made before notes could be edited,
        `updated_at` for one made before memos recorded when they last changed,
        `recorded_at` for one made before an import could be recognised as a
        recording already stored, and `archived_at` for one made before a memo
        could be put away without being deleted."""
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
        if "recorded_at" not in cols:
            self.con.execute("ALTER TABLE memos ADD COLUMN recorded_at TEXT")
        if "archived_at" not in cols:
            self.con.execute("ALTER TABLE memos ADD COLUMN archived_at TEXT")

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

    def data_version(self) -> int:
        """A number sqlite moves whenever some *other* connection commits to
        this database, and leaves alone however much this one writes. A screen
        holding a connection open all day has no other way of learning that the
        recorder wrote a memo from a process of its own; comparing this against
        what it last saw answers that in one read, where the alternative is
        re-reading every listing on a timer to see whether anything moved.

        Only the movement means anything. Two connections to the same file
        start from different numbers, so this is worth comparing against
        earlier readings of this same connection and nothing else."""
        return int(self.con.execute("PRAGMA data_version").fetchone()[0])

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
        recorded_at: str | None = None,
    ) -> int:
        """Stores a finished transcription: memo, segments and speakers together.
        When the recording was made is stored beside it where the source said so
        — the fact a later import is recognised by — and left empty where it did
        not, which is not the same as a recording made at no particular time."""
        with self.con:
            cur = self.con.execute(
                "INSERT INTO memos (filename, wav_path, duration_s, language, status,"
                " project, recorded_at) VALUES (?,?,?,?,'transcribed',?,?)",
                (filename, wav_path, duration_s, language, project, recorded_at),
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

    def _narrowed(
        self,
        project: str | None,
        tag: str | None,
        order: str = "created",
        archived: bool = False,
    ) -> tuple[str, tuple]:
        """How a listing is narrowed and ordered, as SQL and the values it
        reads: the plain list and the one carrying each memo's state are
        narrowed and ordered the same ways, and a filter or an order written
        twice is one that comes to differ.

        Every listing is either the live one or the archive drawer, never the
        two merged: a memo put away is meant to be gone from the list, and a
        view showing both would leave the archiving no visible effect at all.

        A memo nobody has touched carries no updated_at at all, so ordering by
        it falls back to created_at through COALESCE — the same rule
        `memo_rows` already applies to the column it shows a memo's update
        time in, so a listing and the column beside it never disagree about
        when an untouched memo last changed. Every ordering breaks ties on id
        DESC, so two memos that land in the same second — sqlite's own
        resolution for these timestamps — still come back in a stable order
        rather than whichever sqlite happens to hand back that run.

        order is a name only this app's own code passes, never one a person
        types, so a value neither order recognises is a caller's bug and
        raises rather than silently falling back to one of the two it does."""
        where = ["archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
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
        # always at least the archived clause, so there is always a WHERE
        clause = " WHERE " + " AND ".join(where)
        if order == "created":
            order_sql = "id DESC"
        elif order == "updated":
            order_sql = "COALESCE(updated_at, created_at) DESC, id DESC"
        else:
            raise ValueError(f"unknown order: {order!r}")
        return clause + " ORDER BY " + order_sql, tuple(params)

    def memos(
        self,
        project: str | None = None,
        tag: str | None = None,
        order: str = "created",
        *,
        archived: bool = False,
    ) -> list[Memo]:
        """Every memo, newest first whichever order asked for, narrowed to a
        project, to a tag its notes carry, or to both at once. Tags are matched
        whole and whatever case they were written in; a memo nobody has
        extracted has no tags to be found by. Archived memos answer only when
        the archive is what was asked for."""
        tail, params = self._narrowed(project, tag, order, archived)
        return [
            _memo(r)
            for r in self.con.execute(f"SELECT {MEMO_COLUMNS} FROM memos" + tail, params)
        ]

    def memo_listings(
        self,
        project: str | None = None,
        tag: str | None = None,
        order: str = "created",
        *,
        archived: bool = False,
    ) -> list[MemoListing]:
        """Every memo a list shows, narrowed and ordered the same ways as
        `memos`, each one carrying how many voices are in it, how much of what
        it committed to is still outstanding, and whether its transcript has
        been repaired or its notes written by hand.

        One statement however many memos come back: a screen draws the whole
        list at once and redraws it after every job, so counting each row's
        speakers, to-dos and repairs on its own would cost a query per row on
        screen."""
        tail, params = self._narrowed(project, tag, order, archived)
        rows = self.con.execute(
            f"SELECT {MEMO_COLUMNS},"
            " (SELECT count(*) FROM speakers s WHERE s.memo_id=memos.id) AS speakers,"
            # what is left to do, not what was ever committed to: a memo whose
            # tasks are all checked off reads as finished, like one with none
            " (SELECT count(*) FROM todos t WHERE t.memo_id=memos.id"
            "  AND t.status='open') AS open_todos,"
            " EXISTS(SELECT 1 FROM segments g WHERE g.memo_id=memos.id"
            "  AND g.refined_text IS NOT NULL) AS refined,"
            # an emptied note is no more a hand-written one than a missing note
            " (notes_md IS NOT NULL AND notes_md<>'') AS edited"
            " FROM memos" + tail,
            params,
        )
        return [
            MemoListing(
                _memo(r),
                r["speakers"],
                bool(r["refined"]),
                bool(r["edited"]),
                r["open_todos"],
            )
            for r in rows
        ]

    def projects(self) -> list[tuple[str, int]]:
        """Every project that has memos in it and how many, for a sidebar to
        show without a second query per project. Archived memos are counted in
        no project: what the sidebar promises is what opening the project
        shows, and a count including memos the listing hides would send someone
        to a shorter list than the number beside its name."""
        return [
            (r["project"], r["n"])
            for r in self.con.execute(
                "SELECT project, count(*) AS n FROM memos WHERE archived_at IS NULL"
                " GROUP BY project ORDER BY project"
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

    def set_filename(self, memo_id: int, filename: str) -> None:
        """Gives a memo a different name to be listed and found under."""
        with self.con:
            self.con.execute(
                "UPDATE memos SET filename=? WHERE id=?", (filename, memo_id)
            )
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

    def memo_by_recorded_at(self, recorded_at: str) -> Memo | None:
        """The memo already stored for a recording made at this instant, or
        nothing when none is. The newest wins where several carry the stamp: a
        duplicate found twice over is answered with the copy that reflects
        whatever has been done to the memo since.

        A memo stored without the stamp never matches, however the value is
        asked for. Not knowing when a recording was made is no evidence that it
        is this one, and sqlite agreeing — NULL is equal to nothing — is what
        keeps every memo from before this was recorded out of the way."""
        row = self.con.execute(
            f"SELECT {MEMO_COLUMNS} FROM memos WHERE recorded_at=? ORDER BY id DESC LIMIT 1",
            (recorded_at,),
        ).fetchone()
        return _memo(row) if row else None

    def set_archived(self, memo_id: int, archived: bool) -> bool:
        """Puts a memo away or takes it back out, reporting whether that id was
        there to move. When rather than whether: the stamp costs what a flag
        would and says how long ago somebody decided they were done with this,
        which reads beside `updated_at` and `recorded_at` as the same kind of
        fact. Nothing under the memo is rewritten, so what comes back out is
        exactly what went in."""
        row = self.con.execute("SELECT 1 FROM memos WHERE id=?", (memo_id,)).fetchone()
        if row is None:
            return False
        with self.con:
            self.con.execute(
                "UPDATE memos SET archived_at=CASE ? WHEN 1 THEN datetime('now') END"
                " WHERE id=?",
                (int(archived), memo_id),
            )
            # a list ordered by update has to show this move like any other
            self._touch(memo_id)
        return True

    def archived_count(self) -> int:
        """How many memos are in the archive. A sidebar shows the drawer with
        its size beside it whether or not anybody opens it, and that is one
        count rather than a listing nothing on screen would read."""
        return int(
            self.con.execute(
                "SELECT count(*) FROM memos WHERE archived_at IS NOT NULL"
            ).fetchone()[0]
        )

    def delete_memo(self, memo_id: int) -> None:
        """Removes a memo and everything stored under it. One statement is the
        whole deletion: segments, extractions, speakers and to-dos all reference
        the memo with ON DELETE CASCADE, so sqlite carries them off with the row
        and no child table can be left holding rows nothing points at."""
        with self.con:
            self.con.execute("DELETE FROM memos WHERE id=?", (memo_id,))

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

    # --- to-dos ----------------------------------------------------------

    def sync_todos(self, memo_id: int, action_items: Sequence[ActionItem]) -> None:
        """Brings a memo's to-do list into line with the notes just extracted,
        keeping whatever the user has already made of it. The extraction's own
        write is what stamps the memo as changed, so nothing here does it
        again."""
        with self.con:
            self._ingest_todos(memo_id, action_items)

    def _ingest_todos(self, memo_id: int, action_items: Sequence[ActionItem]) -> None:
        """One memo's reconciling, planned and then applied. Kept apart from the
        transaction around it so that filling a whole database in from the
        extractions it already holds is one transaction rather than one per
        memo."""
        plan = reconcile(
            todo_items(action_items),
            self._stored_todos(memo_id),
            self._open_in_project(memo_id),
        )
        self.con.executemany(
            "DELETE FROM todos WHERE id=?", [(todo_id,) for todo_id in plan.delete]
        )
        self.con.executemany(
            "UPDATE todos SET text=?, owner=?, deadline=? WHERE id=?",
            [(i.text, i.owner, i.deadline, todo_id) for todo_id, i in plan.refresh],
        )
        self.con.executemany(
            "INSERT INTO todos (memo_id, text, norm, owner, deadline) VALUES (?,?,?,?,?)",
            [(memo_id, i.text, i.norm, i.owner, i.deadline) for i in plan.insert],
        )

    def _stored_todos(self, memo_id: int) -> list[StoredTodo]:
        """What a memo's to-do list already holds, as planning reads it."""
        return [
            StoredTodo(r["id"], r["norm"], bool(r["touched"]))
            for r in self.con.execute(
                "SELECT id, norm, touched FROM todos WHERE memo_id=?", (memo_id,)
            )
        ]

    def _open_in_project(self, memo_id: int) -> set[str]:
        """What is still outstanding elsewhere in this memo's project, by the key
        to-dos are matched on. A project is a value on the memo rather than on
        the to-do, so the memos are what this reads it through — and a memo moved
        to another project takes its to-dos into that project's reckoning."""
        return {
            r["norm"]
            for r in self.con.execute(
                "SELECT t.norm FROM todos t JOIN memos m ON m.id=t.memo_id"
                " WHERE t.status='open' AND t.memo_id<>?"
                " AND m.project=(SELECT project FROM memos WHERE id=?)",
                (memo_id, memo_id),
            )
        }

    def _backfill_todos(self) -> None:
        """Takes every extraction already stored through the same reconciling a
        fresh one goes through, so a database written before to-dos were tracked
        arrives holding the list its notes always implied. Memos are read in the
        order they were recorded, so which memo in a project keeps a task
        repeated across several of them is decided by which came first rather
        than by the order sqlite happened to hand the rows back."""
        with self.con:
            for row in self.con.execute(
                "SELECT memo_id, json FROM extractions ORDER BY memo_id"
            ):
                data = json.loads(row["json"])
                # a stored extraction is whatever a backend produced, and the
                # earliest of them predate action items being part of that
                self._ingest_todos(row["memo_id"], data.get("action_items") or [])

    def set_todo_status(self, todo_id: int, status: str) -> bool:
        """Checks a to-do off or puts it back, reporting whether that id was
        there to change. Either way the row is marked touched: a person's own
        say-so about a task has to outlive the next extraction, which would
        otherwise reword it or drop it as noise."""
        row = self.con.execute(
            "SELECT memo_id FROM todos WHERE id=?", (todo_id,)
        ).fetchone()
        if row is None:
            return False
        with self.con:
            self.con.execute(
                "UPDATE todos SET status=?,"
                " done_at=CASE ? WHEN 'done' THEN datetime('now') END,"
                " touched=1 WHERE id=?",
                (status, status, todo_id),
            )
            # what a list says about the memo changes with this, so the memo has
            # changed as a reader of it sees it
            self._touch(row["memo_id"])
        return True

    def todos(
        self,
        project: str | None = None,
        *,
        include_done: bool = False,
        memo_id: int | None = None,
    ) -> list[Todo]:
        """The to-dos memos have committed to, open ones only unless the caller
        asks for the finished ones too, and narrowed to one project or to a
        single memo's own commitments when asked. Grouped by the memo they came
        out of and in the order they were found in it, which is the order they
        were said in.

        An archived memo's commitments leave the board, which is a listing and
        hides archived work like every other. Asking for one memo's own to-dos
        is direct access instead, like opening its notes: a memo somebody went
        to on purpose showing an empty task list would read as a memo that
        committed to nothing.

        A to-do has no archived state of its own, deliberately: archiving never
        rewrites whether a task is open or done, so a memo taken back out of the
        archive brings its list back exactly as it stood, and the two tables
        have nothing to drift apart about."""
        where = ["t.status='open'"] if not include_done else []
        params: list = []
        if project is not None:
            where.append("m.project=?")
            params.append(project)
        if memo_id is not None:
            where.append("t.memo_id=?")
            params.append(memo_id)
        else:
            where.append("m.archived_at IS NULL")
        clause = " WHERE " + " AND ".join(where) if where else ""
        return [
            Todo(
                r["id"],
                r["memo_id"],
                r["text"],
                r["owner"],
                r["deadline"],
                r["status"],
                r["project"],
            )
            for r in self.con.execute(
                "SELECT t.id, t.memo_id, t.text, t.owner, t.deadline, t.status, m.project"
                " FROM todos t JOIN memos m ON m.id=t.memo_id"
                + clause
                + " ORDER BY t.memo_id, t.id",
                tuple(params),
            )
        ]


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
        row["recorded_at"],
        row["archived_at"],
    )
