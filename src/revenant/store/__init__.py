"""Journal implementations and the contract they share.

``open_journal`` resolves the store from the environment so that the API, the
CLI and the deploy config all pick one the same way:

    REVENANT_STORE=sqlite   (default)  REVENANT_DB=path
    REVENANT_STORE=mongo               REVENANT_MONGO_URI=...
    REVENANT_STORE=memory              -- tests and the fault sweep
"""

from __future__ import annotations

import os

from .base import (
    ConcurrencyError,
    Journal,
    JournalError,
    RunSummary,
    digest,
    idempotency_key,
    status_from,
)
from .memory import MemoryJournal
from .sqlite import SqliteJournal

__all__ = [
    "ConcurrencyError",
    "Journal",
    "JournalError",
    "MemoryJournal",
    "RunSummary",
    "SqliteJournal",
    "digest",
    "idempotency_key",
    "open_journal",
    "status_from",
]


def open_journal(kind: str | None = None) -> Journal:
    kind = (kind or os.environ.get("REVENANT_STORE") or "sqlite").lower()
    if kind == "memory":
        return MemoryJournal()
    if kind == "sqlite":
        return SqliteJournal(os.environ.get("REVENANT_DB", "revenant.db"))
    if kind == "mongo":
        from .mongo import MongoJournal

        return MongoJournal(os.environ.get("REVENANT_MONGO_URI"))
    raise ValueError(f"unknown store {kind!r}: use sqlite, mongo or memory")
