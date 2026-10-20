"""Start, resume, recover, verify -- the four verbs over a journal.

The engine owns the lifecycle events (``RunStarted``, ``RunResumed``, and the
terminal pair) and nothing else; every decision inside a run belongs to the
workflow and reaches the log through ``RunContext``.

``recover`` is the part that makes the rest worth having. A run whose log has
no terminal event belongs to a process that stopped without saying so, and
those runs are *findable* -- one indexed query -- which is the practical
difference between an event-sourced runtime and a durable queue. The queue
tells you a message was not acknowledged; the log tells you what the agent had
decided, what it had already done, and what it was in the middle of.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from ..events import (
    RunCompleted,
    RunFailed,
    RunResumed,
    RunStarted,
    TERMINAL_KINDS,
)
from ..store.base import ConcurrencyError, Journal, digest
from .context import DivergenceError, ReplayExhausted, RunContext
from .effects import EffectSink, LedgerSink
from .faults import CrashPlan, NoCrash, SimulatedCrash
from .model import Model, ScriptedModel

#: A workflow is a function of (context, input) returning a JSON-able dict.
Workflow = Callable[[RunContext, dict[str, Any]], dict[str, Any]]


class RunNotFound(KeyError):
    pass


class RunAlreadyTerminal(RuntimeError):
    pass


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    events: int = 0
    replayed_events: int = 0
    live_events: int = 0
    model_calls_live: int = 0
    effects_deduplicated: int = 0
    crashed_at: str = ""


@dataclass(slots=True)
class ReplayReport:
    """The verdict of re-executing a finished run against its own log."""

    run_id: str
    deterministic: bool
    identical_bytes: bool
    events_checked: int
    output_matches: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.deterministic and self.identical_bytes and self.output_matches


class Engine:
    def __init__(
        self,
        journal: Journal,
        *,
        model: Model | None = None,
        sink: EffectSink | None = None,
        workflows: dict[str, Workflow] | None = None,
        clock: Callable[[], int] | None = None,
    ):
        self.journal = journal
        self.model = model or ScriptedModel()
        self.sink = sink or LedgerSink()
        self.workflows: dict[str, Workflow] = dict(workflows or {})
        #: Passed through to every context this engine builds. The fault
        #: sweep pins it so two runs of the same workflow are comparable.
        self.clock = clock

    def register(self, name: str, fn: Workflow) -> None:
        self.workflows[name] = fn

    def _workflow(self, name: str) -> Workflow:
        try:
            return self.workflows[name]
        except KeyError:
            raise RunNotFound(f"no workflow named {name!r}") from None

    # -- start / resume -------------------------------------------------

    def start(
        self,
        workflow: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        crash: CrashPlan | None = None,
    ) -> RunResult:
        fn = self._workflow(workflow)
        # The run id is an address, not part of the replayed computation: it
        # seeds the idempotency keys, so it must be unique per run, and on a
        # resume it is read back from the log rather than recomputed.
        run_id = run_id or f"{workflow}-{uuid4().hex[:12]}"
        if self.journal.last_seq(run_id):
            raise RunAlreadyTerminal(f"{run_id} already has a log; resume it instead")
        ctx = RunContext(
            run_id=run_id,
            journal=self.journal,
            model=self.model,
            sink=self.sink,
            crash=crash,
            clock=self.clock,
        )
        ctx._emit(RunStarted(workflow=workflow, input=payload))
        return self._execute(ctx, fn, payload, workflow)

    def resume(self, run_id: str, *, crash: CrashPlan | None = None) -> RunResult:
        log = self.journal.read(run_id)
        if not log:
            raise RunNotFound(run_id)
        if log[-1].kind in TERMINAL_KINDS:
            raise RunAlreadyTerminal(f"{run_id} is {log[-1].kind}")
        started = log[0]
        if started.kind != RunStarted.kind:
            raise RunNotFound(f"{run_id}: log does not begin with RunStarted")
        workflow = started.workflow  # type: ignore[attr-defined]
        payload = dict(started.input)  # type: ignore[attr-defined]
        fn = self._workflow(workflow)
        ctx = RunContext(
            run_id=run_id,
            journal=self.journal,
            model=self.model,
            sink=self.sink,
            crash=crash,
            clock=self.clock,
        )
        ctx._emit(RunResumed(from_seq=log[-1].seq, reason="crash"))
        return self._execute(ctx, fn, payload, workflow)

    def run_to_completion(
        self,
        workflow: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        crash: CrashPlan | None = None,
        max_resumes: int = 64,
    ) -> tuple[RunResult, int]:
        """Start a run, then keep resuming it until it ends.

        The crash plan applies to the first attempt only: a plan that fired on
        every attempt would never finish, and the question being asked is
        "does a run survive *a* crash at this boundary", one boundary at a
        time. Returns the final result and how many resumes it took.
        """
        resumes = 0
        rid = run_id or f"{workflow}-{uuid4().hex[:12]}"
        crashed_at = ""
        try:
            result = self.start(workflow, payload, run_id=rid, crash=crash)
        except SimulatedCrash as crashed:
            # The crash tells us nothing about what survived; the log does.
            crashed_at = str(crashed)
            result = self.status_of(rid)
        while result.status == "interrupted" and resumes < max_resumes:
            resumes += 1
            result = self.resume(rid)
        if result.status == "interrupted":
            raise RuntimeError(f"{rid}: still not terminal after {max_resumes} resumes")
        result.crashed_at = crashed_at
        return result, resumes

    def status_of(self, run_id: str) -> RunResult:
        """Read a run's standing straight off its log.

        Used after a crash, where the in-process result object is gone. The
        three outcomes are all real: ``never-started`` means the crash landed
        before the first append, so there is no run and nothing to recover --
        which is a pass, not a gap.
        """
        log = self.journal.read(run_id)
        if not log:
            return RunResult(run_id=run_id, status="never-started")
        last = log[-1]
        if last.kind == RunCompleted.kind:
            return RunResult(
                run_id=run_id,
                status="completed",
                output=dict(last.output),  # type: ignore[attr-defined]
                events=last.seq,
            )
        if last.kind == RunFailed.kind:
            return RunResult(
                run_id=run_id,
                status="failed",
                error=last.error,  # type: ignore[attr-defined]
                events=last.seq,
            )
        return RunResult(run_id=run_id, status="interrupted", events=last.seq)

    # -- execution ------------------------------------------------------

    def _execute(
        self,
        ctx: RunContext,
        fn: Workflow,
        payload: dict[str, Any],
        workflow: str,
    ) -> RunResult:
        try:
            output = fn(ctx, payload)
        except SimulatedCrash:
            # A crashed process writes nothing. Letting this propagate (rather
            # than recording RunFailed) is what keeps the simulation honest.
            raise
        except ConcurrencyError:
            # Another worker is ahead on this run. Not our run to finish.
            raise
        except Exception as exc:
            ctx._emit(RunFailed(error=f"{type(exc).__name__}: {exc}"))
            return RunResult(
                run_id=ctx.run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                events=self.journal.last_seq(ctx.run_id),
                replayed_events=ctx.replayed_events,
                live_events=ctx.live_events,
                model_calls_live=ctx.model_calls_live,
                effects_deduplicated=ctx.effects_deduplicated,
            )

        if not isinstance(output, dict):
            raise TypeError(
                f"workflow {workflow!r} returned {type(output).__name__}, expected dict"
            )
        ctx._emit(RunCompleted(output=output))
        return RunResult(
            run_id=ctx.run_id,
            status="completed",
            output=output,
            events=self.journal.last_seq(ctx.run_id),
            replayed_events=ctx.replayed_events,
            live_events=ctx.live_events,
            model_calls_live=ctx.model_calls_live,
            effects_deduplicated=ctx.effects_deduplicated,
        )

    # -- recovery -------------------------------------------------------

    def interrupted(self, limit: int = 100) -> list[str]:
        return [s.run_id for s in self.journal.runs(limit) if s.status == "interrupted"]

    def recover(self, limit: int = 100) -> list[RunResult]:
        """Resume every interrupted run. Safe to call from several workers.

        Two workers that pick the same run race on the first append, and the
        journal's unique ``(run_id, seq)`` decides it: the loser gets
        ``ConcurrencyError`` and skips the run rather than running a second
        copy of the agent.
        """
        out = []
        for run_id in self.interrupted(limit):
            try:
                out.append(self.resume(run_id))
            except ConcurrencyError:
                continue
            except RunAlreadyTerminal:
                continue
        return out

    # -- verification ---------------------------------------------------

    def verify_replay(self, run_id: str) -> ReplayReport:
        """Re-execute a finished run against its own log and compare.

        Nothing is written and nothing external is called: the context runs in
        ``verify_only`` mode, where an attempted append is itself the failure.
        Three things are checked, and the first is the one with teeth:

        1. every event the workflow re-derives matches the recorded event
           byte-for-byte (``DivergenceError`` otherwise);
        2. the log is *exactly* consumed -- no recorded event left over, which
           would mean the workflow stopped early;
        3. the recomputed output equals the recorded output.
        """
        log = self.journal.read(run_id)
        if not log:
            raise RunNotFound(run_id)
        if log[-1].kind not in TERMINAL_KINDS:
            raise RunAlreadyTerminal(f"{run_id} has not finished; nothing to verify")
        started = log[0]
        fn = self._workflow(started.workflow)  # type: ignore[attr-defined]

        ctx = RunContext(
            run_id=run_id,
            journal=self.journal,
            model=_RefusingModel(self.model),
            sink=_RefusingSink(),
            crash=NoCrash(),
            verify_only=True,
        )
        recorded_output = (
            dict(log[-1].output) if log[-1].kind == RunCompleted.kind else {}  # type: ignore[attr-defined]
        )
        try:
            output = fn(ctx, dict(started.input))  # type: ignore[attr-defined]
        except DivergenceError as exc:
            return ReplayReport(run_id, False, False, ctx.replayed_events, False, str(exc))
        except ReplayExhausted as exc:
            return ReplayReport(run_id, True, False, ctx.replayed_events, False, str(exc))
        except Exception as exc:  # workflow raised where the record says it did not
            if log[-1].kind == RunFailed.kind:
                same = f"{type(exc).__name__}: {exc}" == log[-1].error  # type: ignore[attr-defined]
                return ReplayReport(
                    run_id,
                    same,
                    same,
                    ctx.replayed_events,
                    same,
                    "" if same else f"re-raised {type(exc).__name__}: {exc}",
                )
            return ReplayReport(
                run_id, False, False, ctx.replayed_events, False, f"raised {exc!r}"
            )

        leftover = ctx.unconsumed()
        return ReplayReport(
            run_id=run_id,
            deterministic=True,
            identical_bytes=not leftover,
            events_checked=ctx.replayed_events,
            output_matches=output == recorded_output,
            detail="" if not leftover else f"{len(leftover)} events never replayed",
        )


class _RefusingModel(Model):
    """Proxies the real model's identity and refuses to call it.

    It has to keep the wrapped model's ``name``, because the name is part of
    the recorded ``ModelCalled`` event -- substituting a stub identity here
    made the first version of this report a divergence against itself. Keeping
    the real name also gives the check a useful meaning it would not otherwise
    have: verification asserts the log replays under *this* runtime's
    configuration, so pointing the engine at a different model and re-running
    the check reports a divergence rather than quietly passing.
    """

    def __init__(self, inner: Model):
        self.inner = inner
        self.name = inner.name

    def complete(self, prompt: str):  # pragma: no cover - guard
        raise ReplayExhausted(
            f"verification tried to call the model ({self.name})"
        )


class _RefusingSink(EffectSink):
    def publish(self, key: str, effect: str, args: dict[str, Any]):  # pragma: no cover
        raise ReplayExhausted(f"verification tried to publish effect {effect!r}")
