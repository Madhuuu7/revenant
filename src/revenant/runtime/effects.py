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
