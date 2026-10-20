"""The event log's alphabet.

Every event is immutable, carries its own ``seq`` within a run, and serialises
to canonical JSON -- sorted keys, no insignificant whitespace, NaN banned.
Canonical form is the point: the replay test asserts the *bytes* of the
decision log match, so an encoder free to reorder keys would make the
strongest assertion in the project meaningless.

Events fall into three groups, and the distinction is the whole design:

* **Decisions** (``StepStarted``, ``RunCompleted``, ...) are produced by the
  agent from state it already has. Replay recomputes them.
* **Observations** (``ModelReturned``, ``EffectObserved``, ``ClockRead``, ...)
  record something only the first execution could know -- a model's sampling,
  a tool's answer, the wall clock. Replay *reads* them instead of asking the
  world again. This is what makes a run deterministic.
* **Intents** (``EffectIntended``) are written *before* the thing they
  describe happens, so a crash in the gap is detectable afterwards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

# Bumped whenever the meaning of an existing event kind changes. A log written
# by an older version is replayed under the version recorded in RunStarted.
LOG_FORMAT_VERSION = 1


class EventDecodeError(ValueError):
    """A stored event did not match any known kind, or lost a field."""


def canonical_json(payload: Any) -> str:
    """Serialise to the one textual form this project considers canonical.

    ``sort_keys`` makes dict order irrelevant, ``separators`` drops the
    whitespace json.dumps adds by default, and ``allow_nan=False`` turns the
    non-standard Infinity/NaN literals into an error rather than a file only
    Python can read back.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class Event:
    """Base class: ``seq`` plus a payload defined by the subclass."""

    kind: ClassVar[str] = "Event"
    #: True when the event records something the agent could not recompute.
    is_observation: ClassVar[bool] = False
    #: True when the event is written before the action it describes runs.
    is_intent: ClassVar[bool] = False

    #: 1-based position in this run's log. Assigned by the journal, not here.
    seq: int = 0

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("seq", None)
        return d

    def to_json(self) -> str:
        return canonical_json({"kind": self.kind, "seq": self.seq, **self.payload()})


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunStarted(Event):
    kind: ClassVar[str] = "RunStarted"
    workflow: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    log_format: int = LOG_FORMAT_VERSION


@dataclass(frozen=True, slots=True)
class StepStarted(Event):
    kind: ClassVar[str] = "StepStarted"
    name: str = ""
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class StepFinished(Event):
    kind: ClassVar[str] = "StepFinished"
    name: str = ""
    state_digest: str = ""


@dataclass(frozen=True, slots=True)
class RunCompleted(Event):
    kind: ClassVar[str] = "RunCompleted"
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunFailed(Event):
    kind: ClassVar[str] = "RunFailed"
    error: str = ""
    #: Set when the failure was raised by the agent rather than the runtime.
    from_agent: bool = True


# --------------------------------------------------------------------------
# observations -- replay reads these instead of re-asking the world
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelCalled(Event):
    kind: ClassVar[str] = "ModelCalled"
    model: str = ""
    #: Hash, not text. The prompt is recomputed on replay and compared against
    #: this; a mismatch means the agent is no longer deterministic (see
    #: runtime.DivergenceError) and is the most useful failure in the system.
    prompt_digest: str = ""


@dataclass(frozen=True, slots=True)
class ModelReturned(Event):
    kind: ClassVar[str] = "ModelReturned"
    is_observation: ClassVar[bool] = True
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ClockRead(Event):
    kind: ClassVar[str] = "ClockRead"
    is_observation: ClassVar[bool] = True
    #: Milliseconds since the epoch, as an int: a float here would make the
    #: canonical encoding platform-dependent.
    epoch_ms: int = 0


@dataclass(frozen=True, slots=True)
class RandomDrawn(Event):
    """A draw from the run's own deterministic stream.

    Classified as a *decision*, not an observation, and that classification is
    the fix for a bug the fault sweep found. When draws were observed -- taken
    from the OS and recorded -- a crash between the draw and its append lost
    the value, the resumed run drew a different one, and in ``triage`` that
    flipped a branch: the recovered run paged an engineer the uninterrupted
    run did not. Deriving the value from ``(run_id, draw_index)`` instead
    makes it recomputable, so losing the event loses nothing. It is still
    logged, because replay then *checks* the recomputed value against the
    recorded one.
    """

    kind: ClassVar[str] = "RandomDrawn"
    #: 63-bit draw, a pure function of the run id and the draw's position.
    value: int = 0
    #: 1-based position in this run's draw stream. Part of the event so that
    #: replay catches a workflow edit that adds or removes a draw.
    index: int = 0


# --------------------------------------------------------------------------
# effects -- the pair that survives a crash in the middle
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectIntended(Event):
    kind: ClassVar[str] = "EffectIntended"
    is_intent: ClassVar[bool] = True
    effect: str = ""
    args_digest: str = ""
    #: Derived from (run_id, seq) and therefore identical across every retry
    #: of this same logical effect. The sink dedupes on it.
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class EffectObserved(Event):
    kind: ClassVar[str] = "EffectObserved"
    is_observation: ClassVar[bool] = True
    effect: str = ""
    idempotency_key: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    #: True when the sink reported this key as already applied -- i.e. the
    #: first attempt did land and we crashed before recording it.
    deduplicated: bool = False


@dataclass(frozen=True, slots=True)
class EffectFailed(Event):
    kind: ClassVar[str] = "EffectFailed"
    is_observation: ClassVar[bool] = True
    effect: str = ""
    idempotency_key: str = ""
    error: str = ""
    #: Retries are a decision, so the retry *count* lives in the log rather
    #: than in runtime memory, which a crash would erase.
    attempt: int = 1


# --------------------------------------------------------------------------
# recovery bookkeeping
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResumed(Event):
    kind: ClassVar[str] = "RunResumed"
    #: How many events existed when recovery picked the run up. Lets the UI
    #: draw the crash boundary and lets tests count resumes.
    from_seq: int = 0
    reason: str = "crash"


_KINDS: dict[str, type[Event]] = {
    cls.kind: cls
    for cls in (
        RunStarted,
        StepStarted,
        StepFinished,
        RunCompleted,
        RunFailed,
        ModelCalled,
        ModelReturned,
        ClockRead,
        RandomDrawn,
        EffectIntended,
        EffectObserved,
        EffectFailed,
        RunResumed,
    )
}

#: Terminal kinds: a log ending in one of these is not resumable.
TERMINAL_KINDS = frozenset({RunCompleted.kind, RunFailed.kind})


def decode(raw: str) -> Event:
    """Rebuild an event from its canonical JSON."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt row
        raise EventDecodeError(f"not JSON: {exc}") from exc
    kind = data.pop("kind", None)
    cls = _KINDS.get(kind)
    if cls is None:
        raise EventDecodeError(f"unknown event kind {kind!r}")
    try:
        return cls(**data)
    except TypeError as exc:
        raise EventDecodeError(f"{kind}: {exc}") from exc


def event_kinds() -> tuple[str, ...]:
    return tuple(_KINDS)
