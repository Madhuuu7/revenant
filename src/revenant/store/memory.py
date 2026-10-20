"""A journal that keeps everything in a dict, for the fault sweep.

This store is honest about what it gives up: it satisfies gapless seq and
atomic append, and it satisfies durability only against the *simulated* crash
the fault injector raises -- a Python exception unwinding the stack -- not
against the process actually dying.

That is the right trade for the sweep in ``scripts/sweep.py``, which runs one
scenario per journal boundary across every workflow and would otherwise pay an
fsync per append on each of them (~150x slower here, which turns a 6-second
check into something nobody runs). The claim that a *real* process death is
survivable is made by ``tests/test_crash_process.py``, which forks an actual
interpreter, kills it with SIGKILL, and recovers from the SQLite journal.

Keeping both and being clear about which proves what is the point; a single
store would have forced a choice between a fast sweep and a believable one.
"""

from __future__ import annotations

import time

from ..events import Event
from .base import ConcurrencyError, Journal, RunSummary, status_from


class MemoryJournal(Journal):
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._created: dict[str, int] = {}
        self._updated: dict[str, int] = {}

    def append(self, run_id: str, event: Event, expected_seq: int) -> Event:
        stream = self._events.setdefault(run_id, [])
        if expected_seq != len(stream) + 1:
            raise ConcurrencyError(
                f"{run_id}: seq {expected_seq} already written"
                if expected_seq <= len(stream)
                else f"{run_id}: seq {expected_seq} leaves a gap after {len(stream)}"
            )
        stamped = type(event)(**{**event.payload(), "seq": expected_seq})
        stream.append(stamped)
        now = int(time.time() * 1000)
        self._created.setdefault(run_id, now)
        self._updated[run_id] = now
        return stamped

    def read(self, run_id: str) -> list[Event]:
        return list(self._events.get(run_id, ()))

    def last_seq(self, run_id: str) -> int:
        return len(self._events.get(run_id, ()))

    def runs(self, limit: int = 100) -> list[RunSummary]:
        out = [self.summary(rid) for rid in self._events]
        out = [s for s in out if s is not None]
        out.sort(key=lambda s: (-s.updated_ms, s.run_id))
        return out[:limit]

    def summary(self, run_id: str) -> RunSummary | None:
        events = self._events.get(run_id)
        if events is None:
            return None
        workflow = next(
            (getattr(e, "workflow", "") for e in events if hasattr(e, "workflow")), ""
        )
        return RunSummary(
            run_id=run_id,
            workflow=workflow,
            status=status_from(events),
            events=len(events),
            created_ms=self._created.get(run_id, 0),
            updated_ms=self._updated.get(run_id, 0),
        )

    def close(self) -> None:
        return None
