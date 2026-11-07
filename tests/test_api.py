"""The HTTP surface, including the parts a reader will click.

The app builds its journal at import time from the environment, which is what
makes the Render start command a one-liner -- so the store is selected here
before the import rather than injected afterwards.
"""

from __future__ import annotations

import os

os.environ["REVENANT_STORE"] = "memory"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from revenant.api.app import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_health_reports_the_store_and_the_workflows(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["store"] == "MemoryJournal"
    assert body["workflows"] == ["onboarding", "research", "triage"]


def test_workflows_lists_boundaries_to_crash_at(client):
    body = client.get("/workflows").json()["workflows"]
    names = [w["name"] for w in body]
    assert names == ["onboarding", "research", "triage"]
    for w in body:
        assert w["boundaries"]
        assert w["boundaries"][0]["index"] == 1
        assert {b["hook"] for b in w["boundaries"]} <= {
            "before_append",
            "after_append",
            "before_effect",
            "after_effect",
        }
        assert w["sample_input"]


def test_start_run_then_read_its_timeline(client):
    body = client.post("/runs", json={"workflow": "onboarding", "run_id": "api-1"}).json()
    assert body["status"] == "completed"
    assert body["output"]["cents"] == 1900

    run = client.get("/runs/api-1").json()
    assert run["status"] == "completed"
    assert len(run["timeline"]) == body["events"]
    groups = {row["group"] for row in run["timeline"]}
    assert groups == {"lifecycle", "decision", "intent", "observation"}
    # Four effects, each with one key, each applied once.
    assert len(run["effects"]) == 4
    assert all(e["applied"] for e in run["effects"])
    assert any(e["costly"] for e in run["effects"])


def test_timeline_rows_carry_a_readable_headline(client):
    client.post("/runs", json={"workflow": "research", "run_id": "api-head"})
    rows = client.get("/runs/api-head").json()["timeline"]
    assert rows[0]["headline"].startswith("run started: research")
    assert any("applied" in r["headline"] for r in rows)
    assert all(r["headline"] for r in rows)


def test_crashing_a_run_on_purpose_leaves_it_interrupted(client):
    body = client.post(
        "/runs", json={"workflow": "research", "run_id": "api-2", "crash_at": 13}
    ).json()
    assert body["status"] == "interrupted"
    assert "simulated crash" in body["crashed_at"]
    assert client.get("/runs/api-2").json()["status"] == "interrupted"


def test_resuming_the_crashed_run_completes_it(client):
    body = client.post("/runs/api-2/resume").json()
    assert body["status"] == "completed"
    assert body["replayed_events"] > 0
    run = client.get("/runs/api-2").json()
    assert run["resumed_at"]  # the resume is marked in the log


def test_verify_reports_a_byte_identical_replay(client):
    body = client.post("/runs/api-2/verify").json()
    assert body["ok"] is True
    assert body["deterministic"] and body["identical_bytes"] and body["output_matches"]
    assert body["events_checked"] > 0


def test_auto_recover_does_the_whole_cycle_in_one_call(client):
    body = client.post(
        "/runs",
        json={
            "workflow": "triage",
            "run_id": "api-3",
            "crash_at": 9,
            "auto_recover": True,
        },
    ).json()
    assert body["status"] == "completed"
    assert body["resumes"] == 1


def test_the_log_is_downloadable_as_canonical_jsonl(client):
    text = client.get("/runs/api-1/log").text
    lines = text.splitlines()
    assert len(lines) == 18
    assert lines[0].startswith("{")
    # Canonical means sorted keys, so "input" precedes "kind".
    assert lines[0].index('"input"') < lines[0].index('"kind"')


def test_recover_sweeps_every_interrupted_run(client):
    client.post("/runs", json={"workflow": "onboarding", "run_id": "api-4", "crash_at": 11})
    body = client.post("/recover").json()
    assert any(r["run_id"] == "api-4" for r in body["recovered"])
    assert body["remaining"] == []


def test_the_effect_ledger_never_shows_a_duplicate(client):
    body = client.get("/effects").json()
    assert body["duplicates"] == {}
    assert body["applied_total"] > 0
    assert all(row["applied"] == 1 for row in body["rows"])


def test_sweep_runs_on_demand_and_passes(client):
    body = client.post("/sweep", params={"workflow": "triage"}).json()
    assert body["ok"] is True
    assert body["workflows"]["triage"]["scenarios"] == 48
    assert body["workflows"]["triage"]["failures"] == []
    assert body["workflows"]["triage"]["dedupe_saves"] > 0


def test_unknown_things_are_404_and_double_starts_are_409(client):
    assert client.post("/runs", json={"workflow": "nope"}).status_code == 404
    assert client.get("/runs/ghost").status_code == 404
    assert client.get("/runs/ghost/log").status_code == 404
    assert client.post("/runs/ghost/resume").status_code == 404
    assert client.post("/sweep", params={"workflow": "nope"}).status_code == 404
    assert (
        client.post("/runs", json={"workflow": "onboarding", "run_id": "api-1"}).status_code
        == 409
    )
    assert client.post("/runs/api-1/resume").status_code == 409


def test_crash_at_zero_is_rejected_by_validation(client):
    assert (
        client.post("/runs", json={"workflow": "triage", "crash_at": 0}).status_code == 422
    )


def test_the_console_is_served_from_the_same_origin(client):
    """The console is a built bundle, and the build is not committed.

    `web/dist` is gitignored, so a fresh checkout -- CI included -- has no
    console until `npm run build` has run. Asserting 200 unconditionally
    passed on a machine with a stale dist lying around and failed on CI for
    a reason that had nothing to do with the runtime. The build is verified
    by the `console` job; what belongs here is the runtime's half of the
    contract.
    """
    from revenant.api.app import CONSOLE_DIR

    resp = client.get("/")
    if not CONSOLE_DIR.is_dir():
        pytest.skip(
            "console not built; run `npm --prefix web install && "
            "npm --prefix web run build`"
        )
    assert resp.status_code == 200
    assert "revenant" in resp.text.lower()


def test_the_api_answers_even_when_the_console_is_missing(client):
    """An unbuilt console must not take the runtime down with it.

    A deploy that skipped the npm step should lose its dashboard, not stop
    recovering runs -- the console is the least important thing this
    process does.
    """
    assert client.get("/health").status_code == 200
    assert client.get("/workflows").status_code == 200
    assert client.post("/sweep?workflow=onboarding").status_code == 200
