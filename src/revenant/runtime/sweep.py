"""The exhaustive fault sweep -- the claim this project is built to support.

The method, in order:

1. Run the workflow once with a recording-only crash plan. Nothing crashes;
   what comes back is the reference output, the reference effect ledger, and
   the list of every boundary the run passed through. **N is measured, not
   chosen** -- picking a few interesting-looking crash points by hand is how
   you end up testing the cases you already thought of.
2. For each boundary k in 1..N, start the run again on a fresh journal and a
   fresh sink with a plan that fires at k, then recover it to completion.
3. Assert four things about the recovered run:

   * it reached ``completed`` (or never started at all, if the crash landed
     before the first append);
   * its output is equal to the reference output;
   * **no idempotency key was applied more than once** -- the exactly-once
     claim, read off the sink's own counter rather than inferred;
   * it replays byte-identically afterwards, so recovery did not leave a log
     that only the crashed process could make sense of.

A failure in any of those is a real bug, and during development three of them
were: an idempotency key recomputed on resume instead of read from the log (so
every post-crash retry published under a new key and the sink could not
dedupe), a ``RunResumed`` event the replay cursor tried to match against
workflow output, and ``SimulatedCrash`` inheriting from ``Exception``, which
let the runtime catch its own simulated crash and write a tidy ``RunFailed``
epitaph a real SIGKILL would never have allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..store.base import Journal
from ..store.memory import MemoryJournal
from .effects import LedgerSink
from .engine import Engine
from .faults import Boundary, CrashPlan, CrashReport
from .model import Model, ScriptedModel


def sweep_run_id(workflow: str) -> str:
    """The run id shared by the reference run and every crash scenario.

    It has to be the same string in all of them. ``ctx.draw`` derives its
    values from the run id, so giving each scenario its own id would make
    every scenario take different branches from the reference and the output
    comparison would be meaningless -- which is exactly what the first version
    of this sweep did. Reusing one id is safe because each scenario runs on a
    fresh journal, so the ids never collide.
    """
    return f"sweep-{workflow}"


@dataclass
class Reference:
    """What a clean run of this workflow looks like."""

    workflow: str
    output: dict[str, Any]
    boundaries: list[Boundary]
    effects_applied: int
    events: int
    effect_keys: int


@dataclass
class SweepResult:
    workflow: str
    reference: Reference
    reports: list[CrashReport] = field(default_factory=list)

    @property
    def scenarios(self) -> int:
        return len(self.reports)

    @property
    def failures(self) -> list[CrashReport]:
        return [r for r in self.reports if not r.ok and r.final_status != "never-started"]

    @property
    def never_started(self) -> int:
        return sum(1 for r in self.reports if r.final_status == "never-started")

    @property
    def resumes(self) -> int:
        return sum(r.resumes for r in self.reports)

    @property
    def dedupe_saves(self) -> int:
        """Scenarios where the sink recognised a duplicate publish.

        These are the crashes that landed *after* the effect reached the world
        and before the journal learned about it. They are the reason the
        idempotency key exists, and counting them is how you show the sweep is
        actually reaching that window rather than only crashing in easy
        places.
        """
        return sum(1 for r in self.reports if "dedupe" in " ".join(r.notes))

    @property
    def ok(self) -> bool:
        return not self.failures


class FrozenClock:
    """Holds the wall clock still for the duration of a scenario.

    The sweep asks one question -- did the *agent* behave differently? -- and
    a moving clock answers a different one. ``now_ms`` is a genuine
    observation: a crash between the read and its append loses the value, the
    resumed run reads the clock again, and it gets a later time. That is not a
    recovery bug, it is the one window event sourcing cannot close for a read,
    and with a live clock it would mask every scenario's real result behind a
    changed timestamp.

    So the sweep freezes time and that window gets a test of its own rather
    than a column in this table: ``tests/test_lost_observation.py`` asserts
    the re-read happens, that the value does change, and that the run still
    completes with its effects applied once.
    """

    __slots__ = ("_at",)

    def __init__(self, at: int = 1_760_000_000_000):
        self._at = at

    def __call__(self) -> int:
        return self._at


def _engine(
    journal: Journal,
    sink: LedgerSink,
    model: Model,
    clock: Callable[[], int] | None = None,
) -> Engine:
    from ..workflows import REGISTRY

    return Engine(journal, model=model, sink=sink, workflows=REGISTRY, clock=clock)


def trace(
    workflow: str,
    payload: dict[str, Any],
    *,
    sink_factory: Callable[[], LedgerSink],
    model_factory: Callable[[], Model] = ScriptedModel,
) -> Reference:
    """One clean run, to learn the reference answer and the boundary count."""
    journal = MemoryJournal()
    sink = sink_factory()
    engine = _engine(journal, sink, model_factory(), FrozenClock())
    plan = CrashPlan(at=None)
    result = engine.start(workflow, payload, run_id=sweep_run_id(workflow), crash=plan)
    if result.status != "completed":
        raise RuntimeError(f"reference run for {workflow} did not complete: {result}")
    return Reference(
        workflow=workflow,
        output=result.output,
        boundaries=plan.boundaries,
        effects_applied=sink.total_applied(),
        events=result.events,
        effect_keys=len(sink.applied),
    )


def run_scenario(
    workflow: str,
    payload: dict[str, Any],
    boundary: Boundary,
    reference: Reference,
    *,
    sink_factory: Callable[[], LedgerSink],
    model_factory: Callable[[], Model] = ScriptedModel,
) -> CrashReport:
    """Crash at one boundary, recover, and check the four properties."""
    journal = MemoryJournal()
    sink = sink_factory()
    engine = _engine(journal, sink, model_factory(), FrozenClock())
    run_id = sweep_run_id(workflow)

    report = CrashReport(boundary=boundary)
    result, resumes = engine.run_to_completion(
        workflow, payload, run_id=run_id, crash=CrashPlan(at=boundary.index)
    )
    report.resumes = resumes
    report.final_status = result.status

    if result.status == "never-started":
        # The crash beat the first append. Nothing durable, nothing external.
        report.output_matches = True
        report.replay_identical = True
        report.effects_exactly_once = sink.total_applied() == 0
        if not report.effects_exactly_once:
            report.notes.append("effect applied with no log to show for it")
        return report

    report.output_matches = result.output == reference.output
    duplicates = sink.duplicates()
    report.effects_exactly_once = not duplicates and (
        sink.total_applied() == reference.effects_applied
    )
    if duplicates:
        report.notes.append(f"duplicate applications: {duplicates}")
    elif sink.total_applied() != reference.effects_applied:
        report.notes.append(
            f"applied {sink.total_applied()} effects, reference applied "
            f"{reference.effects_applied}"
        )

    # Did any publish actually hit the dedupe window? Count it, because a
    # sweep that never reaches that window proves less than it looks like.
    deduped = sum(
        1 for key, n in sink.attempts.items() if n > sink.applied.get(key, 0)
    )
    if deduped:
        report.notes.append(f"dedupe hit on {deduped} key(s)")

    if result.status == "completed":
        replay = engine.verify_replay(run_id)
        report.replay_identical = replay.ok
        if not replay.ok:
            report.notes.append(f"replay: {replay.detail or 'mismatch'}")
    return report


def sweep_workflow(
    workflow: str,
    payload: dict[str, Any] | None = None,
    *,
    sink_factory: Callable[[], LedgerSink] | None = None,
    model_factory: Callable[[], Model] = ScriptedModel,
    progress: Callable[[CrashReport], None] | None = None,
) -> SweepResult:
    from ..workflows import SAMPLE_INPUTS, demo_sink

    payload = payload if payload is not None else SAMPLE_INPUTS[workflow]
    sink_factory = sink_factory or demo_sink
    reference = trace(
        workflow, payload, sink_factory=sink_factory, model_factory=model_factory
    )
    out = SweepResult(workflow=workflow, reference=reference)
    for boundary in reference.boundaries:
        report = run_scenario(
            workflow,
            payload,
            boundary,
            reference,
            sink_factory=sink_factory,
            model_factory=model_factory,
        )
        out.reports.append(report)
        if progress is not None:
            progress(report)
    return out


def sweep_all(
    workflows: list[str] | None = None,
    *,
    progress: Callable[[CrashReport], None] | None = None,
) -> dict[str, SweepResult]:
    from ..workflows import REGISTRY

    names = workflows or list(REGISTRY)
    return {name: sweep_workflow(name, progress=progress) for name in names}
