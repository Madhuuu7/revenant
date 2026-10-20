"""Shared fixtures. Journal-backed tests run against every store that claims
to satisfy the contract, so a store cannot pass by being the one the tests
were written against.
"""

from __future__ import annotations

import pytest

from revenant.runtime import Engine
from revenant.runtime.sweep import FrozenClock
from revenant.store import MemoryJournal, SqliteJournal
from revenant.workflows import REGISTRY, SAMPLE_INPUTS, demo_sink


@pytest.fixture(params=["memory", "sqlite"])
def journal(request, tmp_path):
    if request.param == "memory":
        j = MemoryJournal()
    else:
        j = SqliteJournal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def sink():
    return demo_sink()


@pytest.fixture
def engine(journal, sink):
    """An engine with the clock frozen, so outputs are comparable across runs.

    Freezing time in tests is not hiding anything: the clock's own behaviour
    under a crash is asserted explicitly in test_lost_observation.py.
    """
    return Engine(journal, sink=sink, workflows=REGISTRY, clock=FrozenClock())


@pytest.fixture
def samples():
    return SAMPLE_INPUTS
