"""Deliberate crashes, placed where a crash actually hurts.

Most durability tests kill the process at a convenient moment and conclude the
system is durable. The moments that break event-sourced runtimes are the
inconvenient ones, and there are only four shapes of them. Every journaled
operation passes through these four hooks:

``before_append``
    The decision was made, nothing is on disk. Recovery must re-make it.
``after_append``
    On disk, but the in-process code after the append never ran. Recovery must
    not re-do whatever the event authorised.
``before_effect``
    ``EffectIntended`` is durable, the outside world has not been touched.
    Recovery must retry.
``after_effect``
    The outside world *has* been touched and the journal does not know.
    Recovery must retry and the sink must recognise the duplicate. This is the
    one that makes "exactly-once" a claim about the sink's idempotency rather
    than about the runtime -- see store.base.idempotency_key.

``CrashPlan`` fires on the n-th hook of a run, so a sweep over n = 1..N covers
every boundary a run has. ``trace`` records the boundaries of a clean run,
which is how the sweep learns what N is instead of guessing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class SimulatedCrash(BaseException):
    """Stands in for the process dying.

    Deliberately a ``BaseException``: the runtime catches ``Exception`` to
    record ``RunFailed``, and a simulated crash must not be recorded as the
    agent failing -- a crashed process does not get to write a tidy epitaph.
    Subclassing ``BaseException`` means ``except Exception`` blocks in the
    runtime and in workflow code let it through, which is the behaviour a real
    SIGKILL has.
    """

    def __init__(self, hook: str, index: int, detail: str = ""):
        super().__init__(f"simulated crash at hook #{index} ({hook}) {detail}".strip())
        self.hook = hook
        self.index = index
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Boundary:
    """One place a run can be interrupted."""

    index: int
    hook: str
    detail: str

    def label(self) -> str:
        return f"{self.index}:{self.hook}:{self.detail}"


class CrashPlan:
    """Decides whether a given boundary is the one to crash at.

    ``at=None`` never crashes and just records boundaries, which is how a
    clean run is traced.
    """

    def __init__(self, at: int | None = None):
        self.at = at
        self.count = 0
        self.boundaries: list[Boundary] = []
        self.fired: Boundary | None = None

    def check(self, hook: str, detail: str = "") -> None:
        self.count += 1
        boundary = Boundary(self.count, hook, detail)
        self.boundaries.append(boundary)
        if self.at is not None and self.count == self.at:
            self.fired = boundary
            raise SimulatedCrash(hook, self.count, detail)

    # -- the four hooks, named so stack traces read usefully -------------

    def before_append(self, kind: str) -> None:
        self.check("before_append", kind)

    def after_append(self, kind: str) -> None:
        self.check("after_append", kind)

    def before_effect(self, effect: str) -> None:
        self.check("before_effect", effect)

    def after_effect(self, effect: str) -> None:
        self.check("after_effect", effect)


class NoCrash(CrashPlan):
    """A plan that records nothing and never fires.

    Used in production paths so the hooks cost an attribute lookup and a
    return, not a list append per event.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(at=None)

    def check(self, hook: str, detail: str = "") -> None:
        return None


@dataclass
class CrashReport:
    """What one crash scenario did, for the sweep's output."""

    boundary: Boundary
    resumes: int = 0
    final_status: str = ""
    output_matches: bool = False
    effects_exactly_once: bool = True
    replay_identical: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.final_status == "completed"
            and self.output_matches
            and self.effects_exactly_once
            and self.replay_identical
        )
