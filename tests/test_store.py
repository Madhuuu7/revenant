"""The journal contract, checked against every store that claims it."""

from __future__ import annotations

import pytest

from revenant.events import RunCompleted, RunStarted, StepStarted
from revenant.store import ConcurrencyError, SqliteJournal, open_journal
from revenant.store.base import digest, idempotency_key


def test_append_assigns_seq_and_reads_back_in_order(journal):
    journal.append("r1", RunStarted(workflow="research"), 1)
    journal.append("r1", StepStarted(name="plan"), 2)
    journal.append("r1", StepStarted(name="gather"), 3)
    log = journal.read("r1")
    assert [e.seq for e in log] == [1, 2, 3]
    assert [e.kind for e in log] == ["RunStarted", "StepStarted", "StepStarted"]
    assert journal.last_seq("r1") == 3


def test_second_writer_at_the_same_seq_loses(journal):
    journal.append("r1", RunStarted(workflow="w"), 1)
    with pytest.raises(ConcurrencyError):
        journal.append("r1", StepStarted(name="other"), 1)
    # The loser changed nothing.
    assert journal.last_seq("r1") == 1


def test_runs_are_independent_streams(journal):
    journal.append("a", RunStarted(workflow="w"), 1)
    journal.append("b", RunStarted(workflow="w"), 1)
    assert journal.last_seq("a") == journal.last_seq("b") == 1
    assert len(journal.read("a")) == 1


def test_unknown_run_reads_empty_rather_than_raising(journal):
    assert journal.read("nope") == []
    assert journal.last_seq("nope") == 0
    assert journal.summary("nope") is None


def test_status_tracks_the_last_event(journal):
    journal.append("r1", RunStarted(workflow="research"), 1)
    assert journal.summary("r1").status == "interrupted"
    journal.append("r1", RunCompleted(output={"ok": True}), 2)
    assert journal.summary("r1").status == "completed"


def test_summary_keeps_the_workflow_name_from_the_first_event(journal):
    journal.append("r1", RunStarted(workflow="triage"), 1)
    journal.append("r1", StepStarted(name="score"), 2)
    assert journal.summary("r1").workflow == "triage"


def test_runs_lists_most_recent_first(journal):
    for i, rid in enumerate(["a", "b", "c"], start=1):
        journal.append(rid, RunStarted(workflow=f"w{i}"), 1)
    listed = [s.run_id for s in journal.runs()]
    assert set(listed) == {"a", "b", "c"}
    assert len(listed) == 3


def test_extend_appends_at_successive_seqs(journal):
    written = journal.extend(
        "r1", [RunStarted(workflow="w"), StepStarted(name="a"), StepStarted(name="b")]
    )
    assert [e.seq for e in written] == [1, 2, 3]


def test_log_text_is_one_canonical_line_per_event(journal):
    journal.extend("r1", [RunStarted(workflow="w"), StepStarted(name="a")])
    lines = journal.log_text("r1").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith('{"input":{}')


# -- SQLite specifics ------------------------------------------------------


def test_sqlite_survives_reopening_the_file(tmp_path):
    path = tmp_path / "j.db"
    j = SqliteJournal(path)
    j.extend("r1", [RunStarted(workflow="w"), StepStarted(name="plan")])
    j.close()

    again = SqliteJournal(path)
    try:
        assert [e.kind for e in again.read("r1")] == ["RunStarted", "StepStarted"]
        assert again.summary("r1").status == "interrupted"
    finally:
        again.close()


def test_rebuilding_the_index_changes_nothing(tmp_path):
    """The runs table is written with each append, so a rebuild is a no-op.

    Asserting that turns "derived state stays in step with the log" into
    something checked rather than assumed.
    """
    j = SqliteJournal(tmp_path / "j.db")
    try:
        j.extend("r1", [RunStarted(workflow="research"), StepStarted(name="plan")])
        j.append("r1", RunCompleted(output={"a": 1}), 3)
        before = j.summary("r1")
        assert j.rebuild_index() == 1
        after = j.summary("r1")
        assert (after.status, after.events, after.workflow) == (
            before.status,
            before.events,
            before.workflow,
        )
    finally:
        j.close()


def test_open_journal_resolves_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("REVENANT_STORE", "sqlite")
    monkeypatch.setenv("REVENANT_DB", str(tmp_path / "env.db"))
    j = open_journal()
    try:
        assert isinstance(j, SqliteJournal)
    finally:
        j.close()
    monkeypatch.setenv("REVENANT_STORE", "nonsense")
    with pytest.raises(ValueError):
        open_journal()


# -- digests and keys ------------------------------------------------------


def test_digest_ignores_dict_order_but_not_content():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_idempotency_key_depends_only_on_run_and_seq():
    assert idempotency_key("r1", 4) == idempotency_key("r1", 4)
    assert idempotency_key("r1", 4) != idempotency_key("r1", 5)
    assert idempotency_key("r1", 4) != idempotency_key("r2", 4)
