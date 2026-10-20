"""Start, resume, recover, verify -- including the races and refusals."""

from __future__ import annotations

import pytest

from revenant.runtime import (
    CrashPlan,
    Engine,
    RunAlreadyTerminal,
    RunNotFound,
    SimulatedCrash,
)
from revenant.runtime.effects import LedgerSink
from revenant.runtime.sweep import FrozenClock
from revenant.store import MemoryJournal
from revenant.workflows import REGISTRY, SAMPLE_INPUTS


@pytest.mark.parametrize("workflow", sorted(REGISTRY))
def test_each_workflow_completes_and_verifies(engine, workflow, samples):
    result = engine.start(workflow, samples[workflow], run_id=f"t-{workflow}")
    assert result.status == "completed"
    assert result.output
    assert result.live_events == result.events
    report = engine.verify_replay(f"t-{workflow}")
    assert report.ok, report.detail
    assert report.events_checked > 0


def test_replay_does_not_write_anything(engine, journal, samples):
    engine.start("onboarding", samples["onboarding"], run_id="r")
    before = journal.log_text("r")
    engine.verify_replay("r")
    assert journal.log_text("r") == before


def test_unknown_workflow_is_refused(engine):
    with pytest.raises(RunNotFound):
        engine.start("imaginary", {})


def test_starting_over_an_existing_log_is_refused(engine, samples):
    engine.start("onboarding", samples["onboarding"], run_id="r")
    with pytest.raises(RunAlreadyTerminal):
        engine.start("onboarding", samples["onboarding"], run_id="r")


def test_resuming_a_finished_run_is_refused(engine, samples):
    engine.start("onboarding", samples["onboarding"], run_id="r")
    with pytest.raises(RunAlreadyTerminal):
        engine.resume("r")


def test_resuming_a_run_that_never_existed_is_refused(engine):
    with pytest.raises(RunNotFound):
        engine.resume("ghost")


def test_a_failing_workflow_is_recorded_as_failed(engine, journal):
    def explode(ctx, payload):
        with ctx.step("boom"):
            raise ValueError("deliberate")

    engine.register("explode", explode)
    result = engine.start("explode", {}, run_id="bad")
    assert result.status == "failed"
    assert "ValueError: deliberate" in result.error
    assert journal.read("bad")[-1].kind == "RunFailed"


def test_a_failed_run_also_replays_deterministically(engine):
    def explode(ctx, payload):
        with ctx.step("boom"):
            raise ValueError("deliberate")

    engine.register("explode", explode)
    engine.start("explode", {}, run_id="bad")
    report = engine.verify_replay("bad")
    assert report.ok, report.detail


def test_a_workflow_returning_a_non_dict_is_a_type_error(engine):
    engine.register("wrong", lambda ctx, payload: "not a dict")
    with pytest.raises(TypeError):
        engine.start("wrong", {}, run_id="w")


def test_status_of_reads_the_standing_off_the_log(engine, samples):
    assert engine.status_of("nothing-here").status == "never-started"
    engine.start("triage", samples["triage"], run_id="r")
    assert engine.status_of("r").status == "completed"


def test_interrupted_runs_are_findable_and_recoverable(engine, journal, samples):
    with pytest.raises(SimulatedCrash):
        engine.start("research", samples["research"], run_id="r", crash=CrashPlan(at=9))
    assert engine.interrupted() == ["r"]

    recovered = engine.recover()
    assert [r.status for r in recovered] == ["completed"]
    assert engine.interrupted() == []
    assert engine.verify_replay("r").ok


def test_recovery_is_idempotent(engine, samples):
    with pytest.raises(SimulatedCrash):
        engine.start("triage", samples["triage"], run_id="r", crash=CrashPlan(at=6))
    engine.recover()
    # A second sweep has nothing to do and must not touch the finished run.
    assert engine.recover() == []


def test_a_resume_is_marked_in_the_log(engine, journal, samples):
    with pytest.raises(SimulatedCrash):
        engine.start("onboarding", samples["onboarding"], run_id="r", crash=CrashPlan(at=7))
    engine.resume("r")
    resumed = [e for e in journal.read("r") if e.kind == "RunResumed"]
    assert len(resumed) == 1
    assert resumed[0].from_seq > 0


def test_the_loser_of_a_race_is_told_rather_than_running_a_second_copy(samples):
    """Two engines on one journal: the unique (run_id, seq) index decides."""
    journal = MemoryJournal()
    sink = LedgerSink(handlers={"ping": lambda a: {"ok": 1}})

    def one_effect(ctx, payload):
        with ctx.step("act"):
            ctx.effect("ping", {})
        return {"done": True}

    a = Engine(journal, sink=sink, workflows={"w": one_effect}, clock=FrozenClock())
    b = Engine(journal, sink=sink, workflows={"w": one_effect}, clock=FrozenClock())

    with pytest.raises(SimulatedCrash):
        a.start("w", {}, run_id="r", crash=CrashPlan(at=3))

    assert b.resume("r").status == "completed"
    # `a` picking the same run up afterwards finds it finished, not half-done.
    with pytest.raises(RunAlreadyTerminal):
        a.resume("r")
    assert sink.total_applied() == 1


def test_run_to_completion_reports_how_many_resumes_it_took(engine, samples):
    result, resumes = engine.run_to_completion(
        "research", samples["research"], run_id="r", crash=CrashPlan(at=11)
    )
    assert result.status == "completed"
    assert resumes == 1
    assert "simulated crash" in result.crashed_at


def test_a_crash_before_the_first_append_leaves_no_run(engine, journal, samples):
    result, resumes = engine.run_to_completion(
        "research", samples["research"], run_id="r", crash=CrashPlan(at=1)
    )
    assert result.status == "never-started"
    assert resumes == 0
    assert journal.read("r") == []


def test_verifying_an_unfinished_run_is_refused(engine, samples):
    with pytest.raises(SimulatedCrash):
        engine.start("research", samples["research"], run_id="r", crash=CrashPlan(at=9))
    with pytest.raises(RunAlreadyTerminal):
        engine.verify_replay("r")


def test_an_edited_workflow_fails_verification_instead_of_passing_quietly(engine, samples):
    """Replay is a check on the code, not only on the log.

    Editing a workflow and re-verifying an old run must report a divergence.
    Without this, a resumed run after a deploy would blend two programs.
    """
    engine.start("research", samples["research"], run_id="r")

    original = REGISTRY["research"]

    def edited(ctx, payload):
        payload = dict(payload)
        payload["topic"] = payload.get("topic", "") + " (edited)"
        return original(ctx, payload)

    engine.register("research", edited)
    try:
        report = engine.verify_replay("r")
    finally:
        engine.register("research", original)
    assert not report.ok
    assert not report.deterministic
    assert "diverged" in report.detail
