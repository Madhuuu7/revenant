"""Effect sinks: the only place the runtime is allowed to touch the world.

The uncomfortable fact this module exists to make explicit: **a runtime cannot
give you exactly-once effects.** If the process can die between "send the
email" and "write down that we sent the email", then no amount of logging on
our side distinguishes "sent, not recorded" from "not sent". The two are
indistinguishable from inside.

What a runtime *can* do is make the retry recognisable. Every effect is
published under a key derived only from ``(run_id, seq)``, so the retry after
a crash carries the same key as the attempt that may have landed. A sink that
remembers keys then collapses the duplicate, and the pair -- deterministic key
plus idempotent sink -- is what adds up to exactly-once *application*.

So the guarantee is split and both halves are visible here:

* revenant guarantees at-least-once delivery with a stable key.
* The sink guarantees that one key applies once.

``LedgerSink`` is the reference implementation and the test oracle: it counts
how many times each key was actually applied, which is what lets the fault
sweep assert exactly-once rather than hope for it. A real sink is the same
shape -- Stripe's ``Idempotency-Key`` header, an ``INSERT ... ON CONFLICT DO
NOTHING`` on a unique column, SES with a deduplication id.
"""

from __future__ import annotations

import abc
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from ..store.base import digest


class EffectError(RuntimeError):
    """The sink refused or failed. Retryable unless ``permanent``."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """Result of publishing one effect."""

    result: dict[str, Any]
    #: True when the sink had already applied this key -- i.e. a previous
    #: attempt landed and the crash hid it from us.
    deduplicated: bool


class EffectSink(abc.ABC):
    """An external system that can be written to idempotently."""

    @abc.abstractmethod
    def publish(
        self, key: str, effect: str, args: dict[str, Any]
    ) -> EffectOutcome: ...


@dataclass
class LedgerSink(EffectSink):
    """In-process sink that behaves the way a correct external one has to.

    ``applied`` is the oracle: ``applied[key]`` counts *logical* applications,
    so a key that was published three times across two crashes still reads 1
    if dedupe worked. ``attempts`` counts calls, so a test can tell "deduped"
    apart from "never retried".
    """

    #: name -> handler. A handler receives the args and returns a JSON-able
    #: dict; raising EffectError marks the attempt failed.
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(
        default_factory=dict
    )
    applied: Counter[str] = field(default_factory=Counter)
    attempts: Counter[str] = field(default_factory=Counter)
    #: key -> result, so a duplicate publish returns the original answer
    #: rather than a fresh one. An idempotent sink that returns a *different*
    #: result for the same key is not idempotent in any useful sense.
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Audit trail of applications in order, for the UI's effect ledger.
    journal: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, key: str, effect: str, args: dict[str, Any]) -> EffectOutcome:
        self.attempts[key] += 1
        if key in self.results:
            return EffectOutcome(result=self.results[key], deduplicated=True)

        handler = self.handlers.get(effect)
        if handler is None:
            raise EffectError(f"no handler for effect {effect!r}", permanent=True)
        result = handler(dict(args))
        if not isinstance(result, dict):
            raise EffectError(
                f"effect {effect!r} returned {type(result).__name__}, expected dict",
                permanent=True,
            )

        # Record only after the handler returns: a handler that raises has not
        # applied, and must stay retryable under the same key.
        self.applied[key] += 1
        self.results[key] = result
        self.journal.append(
            {
                "key": key,
                "effect": effect,
                "args_digest": digest(args),
                "result": result,
            }
        )
        return EffectOutcome(result=result, deduplicated=False)

    # -- test oracle ----------------------------------------------------

    def duplicates(self) -> dict[str, int]:
        """Keys applied more than once. Empty dict is the passing case."""
        return {k: n for k, n in self.applied.items() if n > 1}

    def total_applied(self) -> int:
        return sum(self.applied.values())


class SqliteSink(EffectSink):
    """An idempotent sink that survives the process, for the kill test.

    ``LedgerSink`` lives in memory, so it can only prove dedupe within one
    interpreter -- and the interesting case is a process that *died* after
    publishing. This sink is the same contract backed by a file: a UNIQUE
    constraint on the idempotency key, an ``INSERT`` that either wins or
    raises, and the stored result returned on a duplicate.

    That is also what a correct real sink looks like. ``INSERT ... ON CONFLICT
    DO NOTHING`` on a unique column is the entire mechanism behind Stripe's
    ``Idempotency-Key`` header and behind SES deduplication ids; the only
    difference is whose database it is.
    """

    def __init__(
        self,
        path: str,
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ):
        import sqlite3

        self.path = path
        self.handlers = dict(handlers or {})
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS applied ("
            " key TEXT PRIMARY KEY, effect TEXT NOT NULL, result TEXT NOT NULL)"
        )

    def publish(self, key: str, effect: str, args: dict[str, Any]) -> EffectOutcome:
        import json
        import sqlite3

        row = self._conn.execute(
            "SELECT result FROM applied WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return EffectOutcome(result=json.loads(row[0]), deduplicated=True)

        handler = self.handlers.get(effect)
        if handler is None:
            raise EffectError(f"no handler for effect {effect!r}", permanent=True)
        result = handler(dict(args))
        try:
            self._conn.execute(
                "INSERT INTO applied (key, effect, result) VALUES (?, ?, ?)",
                (key, effect, json.dumps(result, sort_keys=True)),
            )
        except sqlite3.IntegrityError:
            # Another worker got there between the SELECT and the INSERT. The
            # key is what resolves it, not a lock.
            row = self._conn.execute(
                "SELECT result FROM applied WHERE key = ?", (key,)
            ).fetchone()
            return EffectOutcome(result=json.loads(row[0]), deduplicated=True)
        return EffectOutcome(result=result, deduplicated=False)

    def applications(self) -> dict[str, int]:
        """key -> 1 for every key that has been applied.

        Counts rows rather than calls, which is the point: the table cannot
        hold two rows for one key, so reading it back after a process death
        answers "how many times did this effect actually apply".
        """
        return {
            k: 1 for (k,) in self._conn.execute("SELECT key FROM applied")
        }

    def close(self) -> None:
        self._conn.close()


@dataclass
class FlakySink(EffectSink):
    """Wraps a sink and fails the first ``failures`` attempts per key.

    Retries and crashes interact badly in exactly one way, and this is how
    that gets tested: a failed attempt must stay retryable under the same key,
    while a *succeeded* attempt must not be re-applied even though the crash
    made it look identical from the runtime's side.
    """

    inner: LedgerSink
    failures: int = 1
    _failed: Counter[str] = field(default_factory=Counter)

    def publish(self, key: str, effect: str, args: dict[str, Any]) -> EffectOutcome:
        if self._failed[key] < self.failures and key not in self.inner.results:
            self._failed[key] += 1
            raise EffectError(f"transient failure {self._failed[key]} for {effect}")
        return self.inner.publish(key, effect, args)
