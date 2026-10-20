"""The one window event sourcing cannot close, pinned by a test.

A read -- the wall clock here -- has to be performed before its value can be
written down. A crash in between loses the value, and the resumed run reads
again and gets a different one. No journal design removes that window for a
read; the alternatives only move it.

So the honest claim is narrower than "a resumed run is identical to an
uninterrupted one", and this test states the narrower claim precisely:

* the crash loses the un-journaled value and the resumed run re-reads it;
* the run still completes, and its effects still apply exactly once;
* it still replays byte-identically afterwards -- the *resumed* log is
  internally consistent, which is what downstream consumers depend on.

``scripts/sweep.py`` freezes the clock for exactly this reason: with a live
clock, every scenario's output would differ for this reason alone and the
sweep's real results would be buried.
"""

from __future__ import annotations

import pytest

from revenant.runtime import CrashPlan, Engine, SimulatedCrash
from revenant.runtime.effects import LedgerSink
from revenant.store import MemoryJournal


def stamped(ctx, payload):
    """Reads the clock, then publishes an effect that depends on it."""
    with ctx.step("stamp"):
        at = ctx.now_ms()
        ctx.state["at"] = at
    with ctx.step("record"):
        ctx.effect("write_row", {"at": at})
    return {"at": at}


@pytest.fixture
def ticking():
    """A clock that advances 1000 ms per read, so a re-read is unmistakable."""
    state = {"t": 1_000_000}

    def clock():
        state["t"] += 1000
        return state["t"]

    return clock


def build(journal, sink, clock):
    return Engine(
        journal, sink=sink, workflows={"stamped": stamped}, clock=clock
    )


def find_boundary(hook: str, detail: str, ticking) -> int:
    """Index of the first boundary matching (hook, detail) in a clean run."""
    plan = CrashPlan(at=None)
    engine = build(
        MemoryJournal(),
        LedgerSink(handlers={"write_row": lambda a: {"row": a["at"]}}),
        ticking,
    )
    engine.start("stamped", {}, run_id="probe", crash=plan)
    for b in plan.boundaries:
        if b.hook == hook and b.detail == detail:
            return b.index
    raise AssertionError(f"no boundary {hook}/{detail}")


def test_a_clock_read_lost_before_its_append_is_re_read(ticking):
    at_index = find_boundary("before_append", "ClockRead", ticking)

    journal = MemoryJournal()
    sink = LedgerSink(handlers={"write_row": lambda a: {"row": a["at"]}})
    engine = build(journal, sink, ticking)

    with pytest.raises(SimulatedCrash):
        engine.start("stamped", {}, run_id="r", crash=CrashPlan(at=at_index))

    # Nothing about the read survived: the log has no ClockRead yet.
    assert [e.kind for e in journal.read("r")] == ["RunStarted", "StepStarted"]

    result = engine.resume("r")
    assert result.status == "completed"

    # The value is a *later* one -- the first read is simply gone.
    recorded = [e for e in journal.read("r") if e.kind == "ClockRead"]
    assert len(recorded) == 1
    assert result.output["at"] == recorded[0].epoch_ms
    assert result.output["at"] > 1_001_000

    # What is still guaranteed: one effect application, and a log that
    # replays byte-identically.
    assert sink.total_applied() == 1
    assert sink.duplicates() == {}
    assert engine.verify_replay("r").ok


def test_a_clock_read_that_reached_the_log_is_replayed_not_re_read(ticking):
    at_index = find_boundary("after_append", "ClockRead", ticking)

    journal = MemoryJournal()
    sink = LedgerSink(handlers={"write_row": lambda a: {"row": a["at"]}})
    engine = build(journal, sink, ticking)

    with pytest.raises(SimulatedCrash):
        engine.start("stamped", {}, run_id="r", crash=CrashPlan(at=at_index))

    recorded = [e for e in journal.read("r") if e.kind == "ClockRead"]
    assert len(recorded) == 1
    original = recorded[0].epoch_ms

    result = engine.resume("r")
    assert result.status == "completed"
    # The resumed run used the recorded instant, not a fresh one.
    assert result.output["at"] == original
    assert len([e for e in journal.read("r") if e.kind == "ClockRead"]) == 1
    assert sink.total_applied() == 1
