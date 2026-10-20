"""The encoding has to be canonical, because replay compares bytes."""

from __future__ import annotations

import json

import pytest

from revenant.events import (
    EffectIntended,
    EffectObserved,
    EventDecodeError,
    ModelReturned,
    RandomDrawn,
    RunStarted,
    TERMINAL_KINDS,
    canonical_json,
    decode,
    event_kinds,
)


def test_key_order_does_not_change_the_bytes():
    a = canonical_json({"b": 1, "a": {"d": 4, "c": 3}})
    b = canonical_json({"a": {"c": 3, "d": 4}, "b": 1})
    assert a == b == '{"a":{"c":3,"d":4},"b":1}'


def test_nan_is_rejected_rather_than_written():
    # json.dumps would happily write NaN, which only Python can read back.
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


@pytest.mark.parametrize(
    "event",
    [
        RunStarted(seq=1, workflow="research", input={"topic": "t", "depth": 2}),
        ModelReturned(seq=4, text="hello", input_tokens=3, output_tokens=2),
        EffectIntended(seq=7, effect="charge_card", args_digest="ab12", idempotency_key="k"),
        EffectObserved(seq=8, effect="charge_card", idempotency_key="k", result={"id": 1}),
        RandomDrawn(seq=9, value=12345, index=1),
    ],
)
def test_roundtrip(event):
    assert decode(event.to_json()) == event


def test_seq_is_in_the_json_but_not_in_the_payload():
    ev = RunStarted(seq=3, workflow="w")
    assert "seq" not in ev.payload()
    assert json.loads(ev.to_json())["seq"] == 3


def test_unknown_kind_is_an_error_not_a_silent_drop():
    with pytest.raises(EventDecodeError):
        decode('{"kind":"Imaginary","seq":1}')


def test_missing_field_is_an_error():
    with pytest.raises(EventDecodeError):
        decode('{"kind":"RunStarted","seq":1,"nonsense":true}')


def test_observation_and_intent_flags_are_set_where_the_design_says():
    assert ModelReturned().is_observation
    assert EffectObserved().is_observation
    assert EffectIntended().is_intent
    # The draw is derived, not observed -- see events.RandomDrawn for why.
    assert not RandomDrawn().is_observation
    assert not RunStarted().is_observation


def test_terminal_kinds_are_the_two_that_end_a_run():
    assert TERMINAL_KINDS == {"RunCompleted", "RunFailed"}
    assert set(TERMINAL_KINDS) <= set(event_kinds())
