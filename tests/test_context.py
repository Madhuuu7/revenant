"""Replay, divergence, and the effect protocol, exercised directly.

These tests drive ``RunContext`` without going through the engine, so that a
failure points at the cursor logic rather than at a workflow.
"""

from __future__ import annotations

import pytest

from revenant.events import RunResumed, RunStarted
from revenant.runtime import DivergenceError, ReplayExhausted, RunContext, ScriptedModel
from revenant.runtime.context import derived_draw
from revenant.runtime.effects import EffectError, LedgerSink
from revenant.store import MemoryJournal


def make_ctx(journal, run_id="r1", sink=None, **kw):
    return RunContext(
        run_id=run_id,
        journal=journal,
        model=ScriptedModel(seed=1),
        sink=sink if sink is not None else LedgerSink(handlers={"ping": lambda a: {"pong": a["n"]}}),
        **kw,
    )


@pytest.fixture
def journal():
    return MemoryJournal()


def test_a_fresh_context_is_not_replaying(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    ctx = make_ctx(journal)
    # RunStarted is the runtime's own bookkeeping, so the cursor skips it and
    # there is nothing of the workflow's to replay.
    assert not ctx.replaying
    assert ctx.finished_replay()


def test_model_call_is_recorded_then_replayed(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    first = make_ctx(journal)
    text = first.call_model("why")
    assert first.model_calls_live == 1

    second = make_ctx(journal)
    assert second.call_model("why") == text
    # The second context did not consult the model at all.
    assert second.model_calls_live == 0
    assert second.replayed_events == 2


def test_a_different_prompt_on_replay_is_a_divergence(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    make_ctx(journal).call_model("original prompt")
    with pytest.raises(DivergenceError) as exc:
        make_ctx(journal).call_model("edited prompt")
    assert "diverged" in str(exc.value)


def test_effect_runs_once_then_returns_the_recorded_result(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    sink = LedgerSink(handlers={"ping": lambda a: {"pong": a["n"]}})
    first = make_ctx(journal, sink=sink)
    assert first.effect("ping", {"n": 3}) == {"pong": 3}

    second = make_ctx(journal, sink=sink)
    assert second.effect("ping", {"n": 3}) == {"pong": 3}
    assert sink.total_applied() == 1
    assert sink.attempts.total() == 1  # the replay did not even call the sink


def test_effect_retries_under_the_same_key_after_a_transient_failure(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] < 3:
            raise EffectError("not yet")
        return {"ok": True}

    sink = LedgerSink(handlers={"ping": flaky})
    ctx = make_ctx(journal, sink=sink)
    assert ctx.effect("ping", {"n": 1}, max_attempts=5) == {"ok": True}
    assert calls["n"] == 3
    assert sink.total_applied() == 1
    # One key across all three attempts: that is what lets a sink dedupe.
    assert len(sink.attempts) == 1
    kinds = [e.kind for e in journal.read("r1")]
    assert kinds.count("EffectFailed") == 2
    assert kinds.count("EffectObserved") == 1


def test_effect_gives_up_after_max_attempts(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    sink = LedgerSink(handlers={"ping": lambda a: (_ for _ in ()).throw(EffectError("no"))})
    ctx = make_ctx(journal, sink=sink)
    with pytest.raises(EffectError):
        ctx.effect("ping", {"n": 1}, max_attempts=2)
    assert [e.kind for e in journal.read("r1")].count("EffectFailed") == 2


def test_a_permanent_failure_is_not_retried(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    ctx = make_ctx(journal, sink=LedgerSink(handlers={}))
    with pytest.raises(EffectError):
        ctx.effect("missing_handler", {}, max_attempts=5)
    assert [e.kind for e in journal.read("r1")].count("EffectFailed") == 1


def test_resumed_effect_reuses_the_recorded_key(journal):
    """The key must come off the log, not be recomputed.

    After a resume the log has grown, so recomputing from the next free seq
    would produce a different key -- and the sink could never match the retry
    against the attempt that may already have landed. This is the bug the
    fault sweep caught first.
    """
    journal.append("r1", RunStarted(workflow="w"), 1)
    sink = LedgerSink(handlers={"ping": lambda a: {"pong": 1}})
    ctx = make_ctx(journal, sink=sink)
    ctx.effect("ping", {"n": 1})
    original_key = next(iter(sink.applied))

    # Simulate the gap: drop the observation so only the intent survives.
    surviving = [e for e in journal.read("r1") if e.kind != "EffectObserved"]
    fresh = MemoryJournal()
    for i, ev in enumerate(surviving, start=1):
        fresh.append("r1", ev, i)
    # The resume marker is what shifts the next free seq, which is precisely
    # why a recomputed key would be wrong here.
    fresh.append("r1", RunResumed(from_seq=len(surviving)), len(surviving) + 1)

    resumed = make_ctx(fresh, sink=sink)
    resumed.effect("ping", {"n": 1})
    assert list(sink.applied) == [original_key]
    assert sink.applied[original_key] == 1
    assert resumed.effects_deduplicated == 1


def test_draw_is_derived_so_two_contexts_agree(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    a = make_ctx(journal)
    values = [a.draw(100) for _ in range(3)]

    replayed = make_ctx(journal)
    assert [replayed.draw(100) for _ in range(3)] == values
    assert replayed.live_events == 0


def test_draw_differs_between_runs_but_not_within_one():
    assert derived_draw("run-a", 1) != derived_draw("run-b", 1)
    assert derived_draw("run-a", 1) != derived_draw("run-a", 2)
    assert derived_draw("run-a", 1) == derived_draw("run-a", 1)


def test_draw_rejects_a_non_positive_bound(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    with pytest.raises(ValueError):
        make_ctx(journal).draw(0)


def test_clock_is_recorded_and_replayed(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    ticks = iter([111, 222])
    first = make_ctx(journal, clock=lambda: next(ticks))
    assert first.now_ms() == 111

    second = make_ctx(journal, clock=lambda: next(ticks))
    assert second.now_ms() == 111  # recorded, not re-read


def test_verify_only_refuses_to_write(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    ctx = make_ctx(journal, verify_only=True)
    with pytest.raises(ReplayExhausted):
        ctx.call_model("anything")


def test_step_brackets_the_work_and_digests_the_state(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    ctx = make_ctx(journal)
    with ctx.step("plan"):
        ctx.state["x"] = 1
    kinds = [e.kind for e in journal.read("r1")]
    assert kinds == ["RunStarted", "StepStarted", "StepFinished"]
    assert journal.read("r1")[-1].state_digest


def test_unconsumed_reports_what_replay_left_behind(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    writer = make_ctx(journal)
    with writer.step("a"):
        pass
    with writer.step("b"):
        pass

    partial = make_ctx(journal)
    with partial.step("a"):
        pass
    assert [e.kind for e in partial.unconsumed()] == ["StepStarted", "StepFinished"]
    assert not partial.finished_replay()
