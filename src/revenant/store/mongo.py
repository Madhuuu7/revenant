"""The MongoDB journal, for the deployment where the runtime is not alone.

SQLite is the better default when one process owns the log. The moment two
workers can pick up the same interrupted run -- which is the normal shape of a
recovery sweep in production -- the journal has to arbitrate between them from
a shared server, and that is what this store is for.

The arbitration is a unique compound index on ``(run_id, seq)``. An append is
a single ``insert_one``; if the pair exists, Mongo returns E11000 and this
store raises ``ConcurrencyError``, so the loser of a race finds out from the
database rather than from a corrupted run. There is no transaction and none is
needed: one document is one event, and a single-document insert is atomic even
on a standalone server (multi-document transactions need a replica set, which
Atlas's free tier does give you, but depending on it here would be depending
on it for nothing).

Writes use ``w="majority"`` for the same reason SQLite uses
``synchronous=FULL``: an append that is acknowledged before it is replicated
can be rolled back by a failover, and "durable except during the failure we
are designed around" is not durable.

Runs against a local ``mongod`` or MongoDB Atlas free tier via
``REVENANT_MONGO_URI``. ``pymongo`` is an optional dependency, so the import
lives inside ``__init__`` -- installing revenant without Mongo must not break
``import revenant.store``.
"""

from __future__ import annotations

import os
import time

from ..events import Event, decode
from .base import ConcurrencyError, Journal, RunSummary, status_from

DEFAULT_URI = "mongodb://127.0.0.1:27017"


class MongoJournal(Journal):
    def __init__(
        self,
        uri: str | None = None,
        *,
        database: str = "revenant",
        timeout_ms: int = 3000,
    ):
        try:
            from pymongo import ASCENDING, MongoClient
            from pymongo.errors import DuplicateKeyError
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "MongoJournal needs pymongo: pip install -e '.[mongo]'"
            ) from exc

        self._duplicate_key = DuplicateKeyError
        self._client = MongoClient(
            uri or os.environ.get("REVENANT_MONGO_URI") or DEFAULT_URI,
            serverSelectionTimeoutMS=timeout_ms,
            w="majority",
        )
        db = self._client[database]
        self._events = db.events
        self._runs = db.runs
        self._events.create_index(
            [("run_id", ASCENDING), ("seq", ASCENDING)], unique=True, name="run_seq"
        )
        self._runs.create_index([("updated_ms", -1)], name="recent")

    def append(self, run_id: str, event: Event, expected_seq: int) -> Event:
        stamped = type(event)(**{**event.payload(), "seq": expected_seq})
        now = int(time.time() * 1000)
        try:
            self._events.insert_one(
                {
                    "_id": f"{run_id}/{expected_seq}",
                    "run_id": run_id,
                    "seq": expected_seq,
                    "kind": stamped.kind,
                    "body": stamped.to_json(),
                    "written_ms": now,
                }
            )
        except self._duplicate_key as exc:
            raise ConcurrencyError(
                f"{run_id}: seq {expected_seq} already written"
            ) from exc

        # The summary is a cache of the log, so it is updated after the event
        # it describes: losing this write loses nothing a rebuild cannot
        # recover, whereas writing it first would advertise an event that is
        # not in the log yet.
        from ..events import RunCompleted, RunFailed

        if stamped.kind == RunCompleted.kind:
            status = "completed"
        elif stamped.kind == RunFailed.kind:
            status = "failed"
        else:
            status = "interrupted"
        self._runs.update_one(
            {"_id": run_id},
            {
                "$set": {"status": status, "events": expected_seq, "updated_ms": now},
                "$setOnInsert": {
                    "created_ms": now,
                    "workflow": getattr(stamped, "workflow", "") or "",
                },
            },
            upsert=True,
        )
        return stamped

    def read(self, run_id: str) -> list[Event]:
        cur = self._events.find({"run_id": run_id}).sort("seq", 1)
        return [decode(doc["body"]) for doc in cur]

    def last_seq(self, run_id: str) -> int:
        doc = self._events.find_one({"run_id": run_id}, sort=[("seq", -1)])
        return int(doc["seq"]) if doc else 0

    def runs(self, limit: int = 100) -> list[RunSummary]:
        cur = self._runs.find().sort("updated_ms", -1).limit(limit)
        return [self._row(doc) for doc in cur]

    def summary(self, run_id: str) -> RunSummary | None:
        doc = self._runs.find_one({"_id": run_id})
        if doc is None:
            events = self.read(run_id)
            if not events:
                return None
            return RunSummary(run_id, "", status_from(events), len(events), 0, 0)
        return self._row(doc)

    @staticmethod
    def _row(doc: dict) -> RunSummary:
        return RunSummary(
            run_id=doc["_id"],
            workflow=doc.get("workflow", ""),
            status=doc.get("status", "unknown"),
            events=int(doc.get("events", 0)),
            created_ms=int(doc.get("created_ms", 0)),
            updated_ms=int(doc.get("updated_ms", 0)),
        )

    def close(self) -> None:
        self._client.close()
