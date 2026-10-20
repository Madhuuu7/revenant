"""What a journal has to guarantee, and the digests built on top of it.

A journal is an append-only log keyed by ``(run_id, seq)``. Three properties
are load-bearing for everything above it:

1. **Gapless, monotonic seq.** Appends carry the ``seq`` the caller believes
   comes next; a mismatch is a ``ConcurrencyError`` rather than a silent
   overwrite. That is how two runtimes racing on the same run resolve -- one
   of them loses the append and stops, instead of both writing step 7.
2. **Atomic append.** An event is either fully readable afterwards or absent.
   A half-written event would break replay in the one situation replay exists
   for.
3. **Durable append.** When ``append`` returns, the event survives the process
   dying. Without this, "resume after a crash" means "resume after a crash we
   were lucky about".

Anything that satisfies those three can back the runtime; the SQLite and Mongo
stores both do, and the in-memory one deliberately gives up (3) so the fault
sweep can run thousands of scenarios in a few seconds.
"""

from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from ..events import Event, canonical_json


class JournalError(RuntimeError):
    """Base class for journal problems."""


class ConcurrencyError(JournalError):
    """Someone else already wrote the seq we were trying to write.

    Raised on the losing side of a race. The runtime treats it as fatal for
    that worker: another worker is further ahead on this run, and continuing
    would mean two agents acting on the same state.
    """


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The row the dashboard lists, derived from the log but stored with it.

    Status is a function of the log's last event, so in principle it does not
    need storing. It is stored anyway, in the same transaction as the append,
    because listing 500 runs otherwise means reading 500 logs.
    """

    run_id: str
    workflow: str
    status: str
    events: int
    created_ms: int
    updated_ms: int


def digest(payload: Any) -> str:
    """Short, stable content hash of any JSON-able value.

    Twelve hex characters: enough that a collision inside one run's log is not
    a practical concern, short enough to read in a UI timeline. It hashes the
    canonical encoding, so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` agree.
    """
    raw = canonical_json(payload).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=6).hexdigest()


def idempotency_key(run_id: str, seq: int) -> str:
    """The key an effect is published under.

    Deterministic in ``(run_id, seq)`` and nothing else -- not the clock, not
    a uuid4, not an attempt counter. That is the entire trick behind
    "exactly-once": every retry of the same logical effect, in this process or
    in one that starts after a crash, computes the same key, so an idempotent
    sink can recognise the duplicate. Keying on anything that varies between
    attempts would publish twice and have no way to know.
    """
    return hashlib.blake2b(
        f"{run_id}/{seq}".encode("utf-8"), digest_size=10
    ).hexdigest()


class Journal(abc.ABC):
    """Append-only event log, one stream per run."""

    @abc.abstractmethod
    def append(self, run_id: str, event: Event, expected_seq: int) -> Event:
        """Write ``event`` at ``expected_seq`` and return it with seq set.

        Raises ``ConcurrencyError`` when ``expected_seq`` is already taken.
        """

    @abc.abstractmethod
    def read(self, run_id: str) -> list[Event]:
        """Every event for a run, in seq order. Empty list if unknown."""

    @abc.abstractmethod
    def last_seq(self, run_id: str) -> int:
        """Highest seq written for a run, or 0."""

    @abc.abstractmethod
    def runs(self, limit: int = 100) -> list[RunSummary]:
        """Most recently updated runs first."""

    @abc.abstractmethod
    def summary(self, run_id: str) -> RunSummary | None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...

    # -- shared helpers -------------------------------------------------

    def extend(self, run_id: str, events: Iterable[Event]) -> list[Event]:
        """Append several events, each at the next free seq."""
        seq = self.last_seq(run_id)
        written = []
        for ev in events:
            seq += 1
            written.append(self.append(run_id, ev, seq))
        return written

    def log_text(self, run_id: str) -> str:
        """The run's log as canonical JSON lines.

        Used by the replay check: two logs are compared as bytes, and this is
        the byte form being compared.
        """
        return "\n".join(ev.to_json() for ev in self.read(run_id))


def status_from(events: list[Event]) -> str:
    """Map a log to the status the UI shows.

    ``interrupted`` is the interesting one: a log that is non-empty, has no
    terminal event, and therefore belongs to a process that stopped without
    saying so. That is exactly the state recovery looks for.
    """
    from ..events import RunCompleted, RunFailed

    if not events:
        return "unknown"
    last = events[-1]
    if last.kind == RunCompleted.kind:
        return "completed"
    if last.kind == RunFailed.kind:
        return "failed"
    return "interrupted"
