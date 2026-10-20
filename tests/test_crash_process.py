"""Kill a real interpreter mid-effect and recover from the files it left.

Everything else in this suite simulates a crash by raising, which is fast
enough to run 140 scenarios in a tenth of a second but relies on the runtime
not catching its own exception and on the journal's in-memory state being
whatever it was. This test removes both assumptions:

* the child process ends via ``os._exit``, which runs no ``finally`` block, no
  ``atexit`` hook, and flushes no Python buffer -- the same abruptness as
  ``SIGKILL``, and portable to Windows, where ``SIGKILL`` does not exist;
* the journal is SQLite on disk with ``synchronous=FULL``, and the effect sink
  is a *second* file with a UNIQUE key, so "the effect applied once" is read
  back from storage the dead process wrote rather than from a counter in the
  surviving one.

The kill lands in the hardest window on purpose: after ``charge_card`` reached
the sink and before the journal recorded it. The recovered run has no way to
know the charge happened; the idempotency key is what tells the sink.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from revenant.runtime import CrashPlan, Engine
from revenant.runtime.effects import SqliteSink
from revenant.store import SqliteJournal
from revenant.store.memory import MemoryJournal
from revenant.workflows import REGISTRY, SAMPLE_INPUTS, demo_sink

RUN_ID = "killed-run"
PAYLOAD = SAMPLE_INPUTS["onboarding"]

CHILD = textwrap.dedent(
    '''
    """Runs the onboarding workflow and dies abruptly at a chosen boundary."""
    import os
    import sys

    from revenant.runtime import CrashPlan, Engine
    from revenant.runtime.effects import SqliteSink
    from revenant.store import SqliteJournal
    from revenant.workflows import REGISTRY, SAMPLE_INPUTS, demo_sink

    journal_path, sink_path, boundary, run_id = sys.argv[1:5]


    class KillPlan(CrashPlan):
        """No unwinding, no flushing: the process simply stops existing."""

        def check(self, hook, detail=""):
            self.count += 1
            if self.count == self.at:
                os._exit(137)


    journal = SqliteJournal(journal_path)
    sink = SqliteSink(sink_path, handlers=demo_sink().handlers)
    engine = Engine(journal, sink=sink, workflows=REGISTRY)
    engine.start(
        "onboarding",
        SAMPLE_INPUTS["onboarding"],
        run_id=run_id,
        crash=KillPlan(at=int(boundary)),
    )
    print("child finished without being killed", file=sys.stderr)
    sys.exit(0)
    '''
)


def boundary_index(hook: str, detail: str) -> int:
    """Find a boundary by name from a clean, in-memory trace."""
    plan = CrashPlan(at=None)
    engine = Engine(MemoryJournal(), sink=demo_sink(), workflows=REGISTRY)
    engine.start("onboarding", PAYLOAD, run_id="probe", crash=plan)
    for b in plan.boundaries:
        if b.hook == hook and b.detail == detail:
            return b.index
    raise AssertionError(f"no {hook}/{detail} boundary in onboarding")


@pytest.fixture
def victim(tmp_path):
    path = tmp_path / "victim.py"
    path.write_text(CHILD, encoding="utf-8")
    return path


def run_child(victim: Path, journal_path: Path, sink_path: Path, index: int):
    return subprocess.run(
        [sys.executable, str(victim), str(journal_path), str(sink_path), str(index), RUN_ID],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "hook,detail",
    [
        ("after_effect", "charge_card"),  # the money left, the log does not know
        ("before_append", "EffectObserved"),  # same window, one step later
        ("after_append", "EffectIntended"),  # intent durable, nothing sent yet
    ],
)
def test_a_killed_process_is_recovered_with_the_charge_applied_once(
    tmp_path, victim, hook, detail
):
    journal_path = tmp_path / "journal.db"
    sink_path = tmp_path / "sink.db"
    index = boundary_index(hook, detail)

    done = run_child(victim, journal_path, sink_path, index)
    assert done.returncode == 137, f"child was not killed: {done.returncode} {done.stderr}"

    # Recovery starts from nothing but the two files.
    journal = SqliteJournal(journal_path)
    sink = SqliteSink(sink_path, handlers=demo_sink().handlers)
    try:
        assert journal.summary(RUN_ID).status == "interrupted"
        engine = Engine(journal, sink=sink, workflows=REGISTRY)
        assert engine.interrupted() == [RUN_ID]

        result = engine.resume(RUN_ID)
        assert result.status == "completed"
        assert result.output["cents"] == PAYLOAD["cents"]

        # Each key applied exactly once, counted from the sink's own table.
        applications = sink.applications()
        assert len(applications) == 4  # create, charge, provision, notify
        assert all(n == 1 for n in applications.values())

        assert engine.verify_replay(RUN_ID).ok
    finally:
        sink.close()
        journal.close()


def test_the_dedupe_window_is_really_being_hit(tmp_path, victim):
    """The after_effect kill must leave the sink ahead of the journal.

    If it did not, the test above would pass for an uninteresting reason: the
    charge never happened, so of course it was not duplicated. Reading both
    files between the kill and the recovery is what rules that out.
    """
    journal_path = tmp_path / "journal.db"
    sink_path = tmp_path / "sink.db"
    index = boundary_index("after_effect", "charge_card")

    done = run_child(victim, journal_path, sink_path, index)
    assert done.returncode == 137

    journal = SqliteJournal(journal_path)
    sink = SqliteSink(sink_path, handlers=demo_sink().handlers)
    try:
        kinds = [e.kind for e in journal.read(RUN_ID)]
        # The charge's intent is on disk; its result is not.
        assert kinds.count("EffectIntended") == 2  # create_account, charge_card
        assert kinds.count("EffectObserved") == 1  # only create_account's
        # But the sink has applied both: this is the state no runtime can see.
        assert len(sink.applications()) == 2

        engine = Engine(journal, sink=sink, workflows=REGISTRY)
        engine.resume(RUN_ID)
        assert all(n == 1 for n in sink.applications().values())
        observed = [e for e in journal.read(RUN_ID) if e.kind == "EffectObserved"]
        deduped = [e for e in observed if e.deduplicated]
        # The recovered run re-published the charge and was told it was a
        # duplicate -- recorded in the log, so the ledger can show it.
        assert len(deduped) == 1
        assert deduped[0].effect == "charge_card"
    finally:
        sink.close()
        journal.close()


def test_the_sqlite_journal_fsyncs_each_append(tmp_path):
    """Durability is a pragma, so assert the pragma.

    Not a proof of power-loss safety -- nothing short of pulling the plug is
    -- but it does catch the regression that matters: someone setting
    synchronous=NORMAL for speed and quietly turning the project's central
    claim into "durable against a process crash only".
    """
    j = SqliteJournal(tmp_path / "j.db")
    try:
        (mode,) = j._conn.execute("PRAGMA journal_mode").fetchone()
        (sync,) = j._conn.execute("PRAGMA synchronous").fetchone()
        assert mode.lower() == "wal"
        assert sync == 2  # 2 == FULL
    finally:
        j.close()


def test_os_exit_really_skips_cleanup(tmp_path):
    """Guards the premise of the kill test.

    If ``os._exit`` ran ``finally`` blocks, every test in this file would be
    testing graceful shutdown while claiming to test a crash.
    """
    script = tmp_path / "exits.py"
    marker = tmp_path / "cleanup-ran"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            try:
                os._exit(137)
            finally:
                open({str(marker)!r}, "w").write("ran")
            """
        ),
        encoding="utf-8",
    )
    done = subprocess.run([sys.executable, str(script)], capture_output=True)
    assert done.returncode == 137
    assert not os.path.exists(marker)
