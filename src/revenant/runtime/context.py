"""The context a workflow is handed, and the only legal way for it to act.

A workflow is ordinary Python -- no generators, no async, no decorators on
every line. What makes it durable is that the four things it cannot be trusted
with go through this object:

``step``          marks a unit of progress and records a state digest
``call_model``    a read that is recorded once and replayed thereafter
``effect``        a write to the outside world, keyed for dedupe
``now`` / ``draw``  the clock and the dice

Everything else -- arithmetic, string formatting, control flow -- is assumed
to be a pure function of state, and the replay check is what catches it when
that assumption is wrong.

## How one object serves first run, resume and verify

The context holds a cursor into the events already in the log. Whenever the
workflow performs a journaled operation:

* **cursor inside the log** -- the operation already happened. The context
  re-derives the event the workflow would have written now, compares it
  byte-for-byte with the stored one (that comparison *is* the determinism
  check), and returns the recorded answer without touching the world.
* **cursor at the end of the log** -- live execution. Perform it, append.

So a first run is the degenerate case of a resume with an empty log, and
verification is the degenerate case where the cursor never reaches the end.
Three behaviours, one mechanism; a second code path for replay is how these
systems normally end up with replay bugs.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
import hashlib
from typing import Any, Callable, Iterator

from ..events import (
    ClockRead,
    EffectFailed,
    EffectIntended,
    EffectObserved,
    Event,
    ModelCalled,
    ModelReturned,
    RandomDrawn,
    RunResumed,
    StepFinished,
    StepStarted,
)
from ..store.base import Journal, digest, idempotency_key
from .effects import EffectError, EffectSink
from .faults import CrashPlan, NoCrash
from .model import Model

def derived_draw(run_id: str, index: int) -> int:
    """The run's n-th pseudo-random value: a pure function, by design.

    Unpredictable across runs because ``run_id`` is a uuid4, and perfectly
    reproducible within one, which is what replay needs. Not a CSPRNG and not
    for key material -- a workflow that needs unpredictability-after-the-fact
    should reach for ``secrets`` and accept that such a value has to be
    journaled as an effect, not a draw.
    """
    seed = hashlib.blake2b(
        f"{run_id}/draw/{index}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(seed, "big") >> 1  # 63 bits


def _os_clock() -> int:
    """Wall clock in epoch milliseconds, the default source for ``now_ms``."""
    return int(time.time() * 1000)


#: Lifecycle events the *runtime* writes, not the workflow: the start, the
#: resume marker, and the terminal pair. Replay skips them rather than trying
#: to match them against workflow output -- the engine checks the terminal
#: event itself, by comparing the recorded output with the recomputed one.
_BOOKKEEPING = frozenset(
    {"RunStarted", "RunResumed", "RunCompleted", "RunFailed"}
)


class DivergenceError(RuntimeError):
    """The workflow did something different from what the log records.

    This is the error worth having. It means the code is not a pure function
    of its journaled inputs -- it read the clock directly, iterated a set,
    depended on a dict's insertion order from a previous version, or was
    simply edited between the crash and the resume. Without this check the
    runtime would resume *something*, quietly, and the state it rebuilt would
    be a blend of two programs.
    """

    def __init__(self, seq: int, expected: str, actual: str):
        super().__init__(
            f"replay diverged at seq {seq}:\n  recorded: {expected}\n  recomputed: {actual}"
        )
        self.seq = seq
        self.expected = expected
        self.actual = actual


class ReplayExhausted(RuntimeError):
    """Verification mode needed to execute something live."""


class RunContext:
    """Journaled handle on one run."""

    def __init__(
        self,
        *,
        run_id: str,
        journal: Journal,
        model: Model,
        sink: EffectSink,
        crash: CrashPlan | None = None,
        verify_only: bool = False,
        clock: Callable[[], int] | None = None,
    ):
        self.run_id = run_id
        self.journal = journal
        self.model = model
        self.sink = sink
        self.crash = crash or NoCrash()
        self.verify_only = verify_only
        #: Where a live ``now_ms`` reads the time. Injected for the same
        #: reason the draw stream is derived: a workflow that stamps its
        #: output with the
        #: clock would otherwise differ between two runs for a reason that has
        #: nothing to do with crash recovery.
        self._clock = clock or _os_clock

        self._log: list[Event] = journal.read(run_id)
        self._cursor = 0
        self._seq = self._log[-1].seq if self._log else 0
        #: Mutable workflow state. Never persisted: it is rebuilt by replay,
        #: and a persisted copy would be a second source of truth to keep in
        #: step with the log.
        self.state: dict[str, Any] = {}
        #: Counts for the API and the sweep's report.
        self.replayed_events = 0
        self.live_events = 0
        self.model_calls_live = 0
        self.effects_live = 0
        self.effects_deduplicated = 0
        #: How many draws this run has made, counting replayed ones: it is the
        #: index the next value is derived from, so it has to survive a resume
        #: -- which it does, because replay re-executes the draws.
        self._draws = 0

    # -- cursor plumbing ------------------------------------------------

    @property
    def replaying(self) -> bool:
        self._skip_bookkeeping()
        return self._cursor < len(self._log)

    def _skip_bookkeeping(self) -> None:
        while self._cursor < len(self._log) and self._log[self._cursor].kind in _BOOKKEEPING:
            self._cursor += 1

    def _peek(self) -> Event | None:
        self._skip_bookkeeping()
        if self._cursor < len(self._log):
            return self._log[self._cursor]
        return None

    def _take(self, expected: Event) -> Event:
        """Consume the next recorded event, asserting it is what we'd write.

        ``expected`` is the event the workflow would produce right now, with
        ``seq`` filled in from the record so the comparison is about content.
        Comparing canonical JSON rather than selected fields means a field
        added later is covered by this check for free.
        """
        recorded = self._peek()
        if recorded is None:
            raise ReplayExhausted(
                f"{self.run_id}: log ends before {expected.kind} at seq {self._seq + 1}"
            )
        rebuilt = type(expected)(**{**expected.payload(), "seq": recorded.seq})
        if recorded.kind != expected.kind or recorded.to_json() != rebuilt.to_json():
            raise DivergenceError(recorded.seq, recorded.to_json(), rebuilt.to_json())
        self._cursor += 1
        self._seq = max(self._seq, recorded.seq)
        self.replayed_events += 1
        return recorded

    def _take_observation(self, kind: str) -> Event | None:
        """Consume a recorded observation, or return None if it is not there.

        Observations cannot be re-derived, so there is nothing to compare: if
        the next recorded event is of this kind it is the answer, and if the
        log stops here the crash landed mid-operation and the caller decides
        what to do about it.
        """
        recorded = self._peek()
        if recorded is None or recorded.kind != kind:
            return None
        self._cursor += 1
        self._seq = max(self._seq, recorded.seq)
        self.replayed_events += 1
        return recorded

    def _emit(self, event: Event) -> Event:
        """Append an event, with a crash boundary on each side of the write."""
        if self.verify_only:
            raise ReplayExhausted(
                f"{self.run_id}: verification wanted to write {event.kind}"
            )
        self.crash.before_append(event.kind)
        written = self.journal.append(self.run_id, event, self._seq + 1)
        self._seq = written.seq
        self.live_events += 1
        self.crash.after_append(event.kind)
        return written

    # -- journaled operations -------------------------------------------

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Bracket a unit of progress.

        Steps are not transactions -- a crash inside one does not roll
        anything back, because the events already written inside it are facts.
        What they give is a resume boundary that is meaningful to read, and a
        state digest that makes "the resumed run rebuilt the same state" an
        assertion rather than a belief.
        """
        started = StepStarted(name=name, attempt=1)
        if self.replaying and self._peek() is not None and self._peek().kind == StepStarted.kind:
            self._take(started)
        else:
            self._emit(started)
        yield
        finished = StepFinished(name=name, state_digest=digest(self.state))
        if self.replaying and self._peek() is not None and self._peek().kind == StepFinished.kind:
            self._take(finished)
        else:
            self._emit(finished)

    def call_model(self, prompt: str) -> str:
        """Ask the model, or recall what it said.

        The ``ModelCalled`` / ``ModelReturned`` pair exists so a crash in the
        gap is visible. The gap is resolved by *calling again*: a model call
        is a read, so a duplicate costs tokens rather than correctness. That
        is a deliberate difference from ``effect``, which must never be
        blindly re-run.
        """
        called = ModelCalled(model=self.model.name, prompt_digest=digest(prompt))
        if self.replaying and self._peek() is not None and self._peek().kind == ModelCalled.kind:
            self._take(called)
            recorded = self._take_observation(ModelReturned.kind)
            if recorded is not None:
                return recorded.text  # type: ignore[attr-defined]
            # Crash between call and response: retry it live.
        else:
            self._emit(called)

        result = self.model.complete(prompt)
        self.model_calls_live += 1
        self._emit(
            ModelReturned(
                text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        )
        return result.text

    def effect(
        self, name: str, args: dict[str, Any], *, max_attempts: int = 3
    ) -> dict[str, Any]:
        """Do something the outside world can see, at most once.

        The sequence is intent -> act -> observe, and the key is derived from
        the seq the *intent* occupies, which is why it has to be read back
        from the log on a resume instead of recomputed: by then the log has a
        ``RunResumed`` in it and the next free seq has moved.
        """
        args_digest = digest(args)
        intent = EffectIntended(effect=name, args_digest=args_digest, idempotency_key="")

        recorded_intent = None
        peeked = self._peek()
        if self.replaying and peeked is not None and peeked.kind == EffectIntended.kind:
            # Compare on (effect, args) only -- the key is the record's to
            # own, and filling it in from the record is what makes the retry
            # carry the original key.
            intent = EffectIntended(
                effect=name,
                args_digest=args_digest,
                idempotency_key=peeked.idempotency_key,  # type: ignore[attr-defined]
            )
            recorded_intent = self._take(intent)
            key = recorded_intent.idempotency_key  # type: ignore[attr-defined]
        else:
            key = idempotency_key(self.run_id, self._seq + 1)
            self._emit(
                EffectIntended(effect=name, args_digest=args_digest, idempotency_key=key)
            )

        # Replay whatever attempts are already recorded for this effect.
        attempt = 1
        while True:
            observed = self._take_observation(EffectObserved.kind)
            if observed is not None:
                if observed.deduplicated:  # type: ignore[attr-defined]
                    self.effects_deduplicated += 1
                return dict(observed.result)  # type: ignore[attr-defined]
            failed = self._take_observation(EffectFailed.kind)
            if failed is None:
                break
            attempt = failed.attempt + 1  # type: ignore[attr-defined]

        # Live attempts, all under the one key.
        while True:
            self.crash.before_effect(name)
            try:
                outcome = self.sink.publish(key, name, args)
            except EffectError as exc:
                self._emit(
                    EffectFailed(
                        effect=name,
                        idempotency_key=key,
                        error=str(exc),
                        attempt=attempt,
                    )
                )
                if exc.permanent or attempt >= max_attempts:
                    raise
                attempt += 1
                continue
            self.crash.after_effect(name)
            self.effects_live += 1
            if outcome.deduplicated:
                self.effects_deduplicated += 1
            self._emit(
                EffectObserved(
                    effect=name,
                    idempotency_key=key,
                    result=outcome.result,
                    deduplicated=outcome.deduplicated,
                )
            )
            return dict(outcome.result)

    def now_ms(self) -> int:
        """Wall clock, recorded. Replay gets the original instant."""
        recorded = None
        if self.replaying:
            recorded = self._take_observation(ClockRead.kind)
        if recorded is not None:
            return recorded.epoch_ms  # type: ignore[attr-defined]
        value = self._clock()
        self._emit(ClockRead(epoch_ms=value))
        return value

    def draw(self, upper: int) -> int:
        """A pseudo-random integer in [0, upper), derived and then checked.

        Workflows must take randomness from here. A ``random.random()`` call
        inside workflow code replays to a different value and the divergence
        check turns it into a loud failure -- which is the designed outcome,
        not a limitation.

        Unlike the clock, this is *derived* rather than observed, so there is
        no window in which a crash can lose it. That was not the first design;
        see ``events.RandomDrawn`` for the branch-flipping bug that changed
        it.
        """
        if upper <= 0:
            raise ValueError("upper must be positive")
        self._draws += 1
        value = derived_draw(self.run_id, self._draws)
        event = RandomDrawn(value=value, index=self._draws)
        peeked = self._peek()
        if self.replaying and peeked is not None and peeked.kind == RandomDrawn.kind:
            self._take(event)
        else:
            self._emit(event)
        return value % upper

    # -- introspection --------------------------------------------------

    def finished_replay(self) -> bool:
        """True when every recorded event has been accounted for."""
        self._skip_bookkeeping()
        return self._cursor >= len(self._log)

    def unconsumed(self) -> list[Event]:
        self._skip_bookkeeping()
        return self._log[self._cursor :]
