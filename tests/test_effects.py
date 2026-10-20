"""Sink behaviour, since half the exactly-once guarantee lives there."""

from __future__ import annotations

import pytest

from revenant.runtime.effects import (
    EffectError,
    FlakySink,
    LedgerSink,
    SqliteSink,
)


@pytest.fixture
def handlers():
    return {"ping": lambda a: {"pong": a.get("n", 0)}}


def test_the_same_key_applies_once_and_returns_the_first_answer(handlers):
    sink = LedgerSink(handlers=handlers)
    first = sink.publish("k1", "ping", {"n": 1})
    second = sink.publish("k1", "ping", {"n": 99})

    assert not first.deduplicated
    assert second.deduplicated
    # Same key, different args: the sink returns what it did the first time
    # rather than acting on the new args. An idempotent sink that answers
    # differently for one key is not idempotent in any useful sense.
    assert second.result == {"pong": 1}
    assert sink.applied["k1"] == 1
    assert sink.attempts["k1"] == 2


def test_different_keys_are_different_effects(handlers):
    sink = LedgerSink(handlers=handlers)
    sink.publish("k1", "ping", {"n": 1})
    sink.publish("k2", "ping", {"n": 1})
    assert sink.total_applied() == 2
    assert sink.duplicates() == {}


def test_a_missing_handler_is_permanent(handlers):
    sink = LedgerSink(handlers=handlers)
    with pytest.raises(EffectError) as exc:
        sink.publish("k", "unknown", {})
    assert exc.value.permanent


def test_a_handler_returning_a_non_dict_is_rejected():
    sink = LedgerSink(handlers={"bad": lambda a: "nope"})
    with pytest.raises(EffectError):
        sink.publish("k", "bad", {})


def test_a_failed_attempt_stays_retryable_under_the_same_key(handlers):
    """A raising handler has not applied, so the key must stay open."""
    sink = LedgerSink(handlers=handlers)
    flaky = FlakySink(inner=sink, failures=2)
    with pytest.raises(EffectError):
        flaky.publish("k", "ping", {"n": 5})
    with pytest.raises(EffectError):
        flaky.publish("k", "ping", {"n": 5})
    outcome = flaky.publish("k", "ping", {"n": 5})
    assert not outcome.deduplicated
    assert sink.applied["k"] == 1


def test_a_succeeded_attempt_is_not_re_applied_even_through_the_wrapper(handlers):
    sink = LedgerSink(handlers=handlers)
    flaky = FlakySink(inner=sink, failures=1)
    with pytest.raises(EffectError):
        flaky.publish("k", "ping", {"n": 1})
    flaky.publish("k", "ping", {"n": 1})
    assert flaky.publish("k", "ping", {"n": 1}).deduplicated
    assert sink.applied["k"] == 1


def test_journal_records_applications_in_order(handlers):
    sink = LedgerSink(handlers=handlers)
    sink.publish("k1", "ping", {"n": 1})
    sink.publish("k1", "ping", {"n": 1})
    sink.publish("k2", "ping", {"n": 2})
    assert [row["key"] for row in sink.journal] == ["k1", "k2"]


# -- the durable sink ------------------------------------------------------


def test_sqlite_sink_dedupes_across_connections(tmp_path, handlers):
    """Dedupe has to survive the process, so it has to survive a reconnect."""
    path = str(tmp_path / "sink.db")
    first = SqliteSink(path, handlers=handlers)
    try:
        assert not first.publish("k", "ping", {"n": 7}).deduplicated
    finally:
        first.close()

    second = SqliteSink(path, handlers=handlers)
    try:
        outcome = second.publish("k", "ping", {"n": 7})
        assert outcome.deduplicated
        assert outcome.result == {"pong": 7}
        assert second.applications() == {"k": 1}
    finally:
        second.close()


def test_sqlite_sink_cannot_hold_two_rows_for_one_key(tmp_path, handlers):
    path = str(tmp_path / "sink.db")
    sink = SqliteSink(path, handlers=handlers)
    try:
        for _ in range(5):
            sink.publish("k", "ping", {"n": 1})
        assert sink.applications() == {"k": 1}
    finally:
        sink.close()


def test_sqlite_sink_rejects_an_unknown_effect(tmp_path):
    sink = SqliteSink(str(tmp_path / "sink.db"))
    try:
        with pytest.raises(EffectError):
            sink.publish("k", "ping", {})
    finally:
        sink.close()
