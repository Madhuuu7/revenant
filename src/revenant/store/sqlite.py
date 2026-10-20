"""The durable journal: SQLite in WAL mode, one row per event.

Why SQLite and not a queue or a document store: the guarantee the runtime
needs is "append at seq N, atomically, and tell me if N is taken". A UNIQUE
index on ``(run_id, seq)`` plus one transaction is the whole implementation,
and it is a file you can hand to someone to inspect.

Two pragmas carry the durability claim and are worth being able to defend:

``journal_mode=WAL``
    Readers do not block the writer, so the dashboard can poll a run's
    timeline while the run is still appending to it.

``synchronous=FULL``
    The WAL is fsynced on every commit. With the default ``NORMAL``, a commit
    is durable against the *process* dying but not against the machine losing
    power -- which would make this project's central claim depend on which
    failure happened. FULL costs a sync per event: 1,259 appends/s against
    13,740 at NORMAL on this machine (``scripts/bench.py``), so 0.79 ms per
    event instead of 0.07 ms. That is 11x, and it is the right trade here --
    an agent run is tens of events, so the whole run spends about 16 ms in
    fsync, while the alternative is a durability claim with an asterisk.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from ..events import Event, decode
from .base import ConcurrencyError, Journal, RunSummary, status_from

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    body     TEXT NOT NULL,
    written_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    workflow   TEXT NOT NULL,
    status     TEXT NOT NULL,
    events     INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS runs_updated ON runs (updated_ms DESC);
"""


class SqliteJournal(Journal):
    """Durable journal. ``path=":memory:"`` is accepted but not durable."""

    def __init__(self, path: str | Path = "revenant.db", *, synchronous: str = "FULL"):
        self.path = str(path)
        # FastAPI runs sync endpoints in a threadpool, so the connection is
        # reached from more than one thread. check_same_thread=False allows
        # that; the lock below is what actually makes it safe, since sqlite3
        # serialises statements but not the BEGIN/INSERT/COMMIT sequence an
        # append is made of.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=30.0, check_same_thread=False
        )
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # -- writes ---------------------------------------------------------

    def append(self, run_id: str, event: Event, expected_seq: int) -> Event:
        stamped = type(event)(**{**event.payload(), "seq": expected_seq})
        now = int(time.time() * 1000)
        with self._lock:
            return self._append_locked(run_id, stamped, expected_seq, now)

    def _append_locked(
        self, run_id: str, stamped: Event, expected_seq: int, now: int
    ) -> Event:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO events (run_id, seq, kind, body, written_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, expected_seq, stamped.kind, stamped.to_json(), now),
            )
            self._touch_run(run_id, stamped, expected_seq, now)
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            raise ConcurrencyError(
                f"{run_id}: seq {expected_seq} already written"
            ) from exc
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return stamped

    def _touch_run(self, run_id: str, event: Event, seq: int, now: int) -> None:
        """Keep the ``runs`` row in step inside the same transaction.

        The status written here is derived from this one event rather than by
        re-reading the log: the only events that end a run are terminal ones,
        so a single event is enough information.
        """
        from ..events import RunCompleted, RunFailed

        if event.kind == RunCompleted.kind:
            status = "completed"
        elif event.kind == RunFailed.kind:
            status = "failed"
        else:
            status = "interrupted"
        workflow = getattr(event, "workflow", "") or ""
        self._conn.execute(
            "INSERT INTO runs (run_id, workflow, status, events, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET"
            "   status=excluded.status,"
            "   events=excluded.events,"
            "   updated_ms=excluded.updated_ms,"
            "   workflow=CASE WHEN runs.workflow='' THEN excluded.workflow"
            "                 ELSE runs.workflow END",
            (run_id, workflow, status, seq, now, now),
        )

    # -- reads ----------------------------------------------------------

    def read(self, run_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [decode(r[0]) for r in rows]

    def last_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0] or 0)

    def runs(self, limit: int = 100) -> list[RunSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, workflow, status, events, created_ms, updated_ms"
                " FROM runs ORDER BY updated_ms DESC, run_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunSummary(*r) for r in rows]

    def summary(self, run_id: str) -> RunSummary | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, workflow, status, events, created_ms, updated_ms"
                " FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return RunSummary(*row) if row else None

    def rebuild_index(self) -> int:
        """Recompute every ``runs`` row from the events table.

        The index is written transactionally with each append, so this should
        be a no-op -- which is why it exists. ``tests/test_store.py`` calls it
        and asserts nothing changed, turning "derived state stays in step with
        the log" into something checked rather than assumed. It is also the
        repair path if a future migration touches the events table directly.
        """
        with self._lock:
            return self._rebuild_locked()

    def _rebuild_locked(self) -> int:
        run_ids = [
            r[0] for r in self._conn.execute("SELECT DISTINCT run_id FROM events")
        ]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for run_id in run_ids:
                events = self.read(run_id)
                times = self._conn.execute(
                    "SELECT MIN(written_ms), MAX(written_ms) FROM events"
                    " WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                workflow = next(
                    (getattr(e, "workflow", "") for e in events if hasattr(e, "workflow")),
                    "",
                )
                self._conn.execute(
                    "INSERT INTO runs (run_id, workflow, status, events, created_ms,"
                    " updated_ms) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,"
                    " events=excluded.events, workflow=excluded.workflow,"
                    " created_ms=excluded.created_ms, updated_ms=excluded.updated_ms",
                    (
                        run_id,
                        workflow,
                        status_from(events),
                        events[-1].seq if events else 0,
                        int(times[0] or 0),
                        int(times[1] or 0),
                    ),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(run_ids)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
