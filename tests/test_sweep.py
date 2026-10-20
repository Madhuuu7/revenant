"""The headline claim, as a test rather than a script.

``scripts/sweep.py`` prints the table for the README; this runs the same sweep
in CI so a regression in recovery fails the build instead of waiting for
someone to run the script.
"""

from __future__ import annotations

import pytest

from revenant.runtime.sweep import sweep_workflow
from revenant.workflows import REGISTRY


@pytest.fixture(scope="module", params=sorted(REGISTRY))
def swept(request):
    return sweep_workflow(request.param)


def test_every_boundary_got_a_scenario(swept):
    assert swept.scenarios == len(swept.reference.boundaries)
    assert swept.scenarios >= 30  # the smallest workflow has this many


def test_every_scenario_recovered(swept):
    assert swept.ok, [
        (r.boundary.label(), r.final_status, r.notes) for r in swept.failures
    ]


def test_every_recovered_run_produced_the_reference_output(swept):
    mismatched = [
        r.boundary.label()
        for r in swept.reports
        if r.final_status == "completed" and not r.output_matches
    ]
    assert mismatched == []


def test_no_effect_was_ever_applied_twice(swept):
    offenders = [
        (r.boundary.label(), r.notes)
        for r in swept.reports
        if not r.effects_exactly_once
    ]
    assert offenders == []


def test_every_recovered_run_replays_byte_identically(swept):
    bad = [
        r.boundary.label()
        for r in swept.reports
        if r.final_status == "completed" and not r.replay_identical
    ]
    assert bad == []


def test_the_sweep_actually_reaches_the_dedupe_window(swept):
    """A sweep that never lands between the effect and its record proves less.

    Counting the scenarios where the sink reported a duplicate is how we know
    the hard window is being exercised and not merely claimed.
    """
    assert swept.dedupe_saves > 0


def test_crashes_before_the_first_append_are_accounted_for_separately(swept):
    # Exactly one boundary can precede the first append: RunStarted's.
    assert swept.never_started == 1


def test_recovery_needed_a_resume_for_nearly_every_scenario(swept):
    # One scenario (the one before the first append) needs no resume; the
    # terminal-append scenarios complete on the crashed attempt's own log.
    assert swept.resumes >= swept.scenarios - 3
