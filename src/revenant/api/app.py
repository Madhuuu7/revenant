"""HTTP surface: start runs, break them on purpose, watch them recover.

Two decisions worth stating.

**Crashing is a first-class API operation.** ``POST /runs`` takes a
``crash_at`` boundary, and ``GET /workflows`` lists the boundaries to choose
from. A durability feature you cannot trigger on demand is a claim in a
README; one that is a button is something a reader can disprove in ten
seconds. The server catches the simulated crash and reports the run as
interrupted, exactly as it would find it after a real process death.

**The timeline is classified, not just listed.** Every event goes out with the
group it belongs to -- decision, observation, intent, lifecycle -- because the
distinction is the thing worth seeing: what replay recomputes, what it reads
back, and what was written down before the world was touched.

The whole app is one origin: the console is served from ``/`` by this same
service, so the deployed demo has no CORS to get wrong and no second free-tier
service to keep awake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..events import Event
from ..runtime.effects import LedgerSink
from ..runtime.engine import Engine, RunAlreadyTerminal, RunNotFound
from ..runtime.faults import CrashPlan, SimulatedCrash
from ..runtime.model import default_model
from ..runtime.sweep import sweep_workflow
from ..store import open_journal
from ..store.base import ConcurrencyError
from ..workflows import COSTLY_EFFECTS, REGISTRY, SAMPLE_INPUTS, demo_sink

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

app = FastAPI(
    title="revenant",
    version="0.1.0",
    description="A durable agent runtime: event-sourced, crash-resumable, "
    "deterministic on replay.",
)

_journal = open_journal()
_sink: LedgerSink = demo_sink()
_engine = Engine(_journal, model=default_model(), sink=_sink, workflows=REGISTRY)


def engine() -> Engine:
    return _engine


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------


class StartRequest(BaseModel):
    workflow: str
    input: dict[str, Any] | None = None
    #: Boundary to crash at, 1-based, as listed by ``GET /workflows``.
    crash_at: int | None = Field(default=None, ge=1)
    #: Resume automatically after the crash, for a one-click demo.
    auto_recover: bool = False
    run_id: str | None = None


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

_GROUPS = {
    "RunStarted": "lifecycle",
    "RunResumed": "lifecycle",
    "RunCompleted": "lifecycle",
    "RunFailed": "lifecycle",
    "StepStarted": "decision",
    "StepFinished": "decision",
    "ModelCalled": "decision",
    "EffectIntended": "intent",
    "RandomDrawn": "decision",
    "ModelReturned": "observation",
    "ClockRead": "observation",
    "EffectObserved": "observation",
    "EffectFailed": "observation",
}

#: One short line per event kind for the timeline. Keeping the phrasing here
#: rather than in the page means the API and any other client agree on what an
#: event means.
def _headline(event: Event) -> str:
    kind = event.kind
    g = lambda name, default="": getattr(event, name, default)  # noqa: E731
    if kind == "RunStarted":
        return f"run started: {g('workflow')}"
    if kind == "RunResumed":
        return f"resumed after {g('reason')} at seq {g('from_seq')}"
    if kind == "RunCompleted":
        return "run completed"
    if kind == "RunFailed":
        return f"run failed: {g('error')}"
    if kind == "StepStarted":
        return f"step {g('name')}"
    if kind == "StepFinished":
        return f"step {g('name')} done - state {g('state_digest')}"
    if kind == "ModelCalled":
        return f"ask {g('model')} - prompt {g('prompt_digest')}"
    if kind == "ModelReturned":
        text = g("text")
        return f"model said: {text[:60]}"
    if kind == "ClockRead":
        return f"clock read {g('epoch_ms')}"
    if kind == "RandomDrawn":
        return f"draw #{g('index')} derived"
    if kind == "EffectIntended":
        return f"about to {g('effect')} - key {g('idempotency_key')[:10]}"
    if kind == "EffectObserved":
        if g("deduplicated"):
            return f"{g('effect')} was already applied - deduplicated"
        return f"{g('effect')} applied"
    if kind == "EffectFailed":
        return f"{g('effect')} attempt {g('attempt')} failed: {g('error')}"
    return kind


def _row(event: Event) -> dict[str, Any]:
    payload = event.payload()
    effect = payload.get("effect", "")
    return {
        "seq": event.seq,
        "kind": event.kind,
        "group": _GROUPS.get(event.kind, "decision"),
        "headline": _headline(event),
        "costly": effect in COSTLY_EFFECTS,
        "deduplicated": bool(payload.get("deduplicated")),
        "payload": payload,
    }


def _result(result) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "events": result.events,
        "replayed_events": result.replayed_events,
        "live_events": result.live_events,
        "model_calls_live": result.model_calls_live,
        "effects_deduplicated": result.effects_deduplicated,
        "crashed_at": result.crashed_at,
    }


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    runs = _journal.runs(500)
    return {
        "status": "ok",
        "store": type(_journal).__name__,
        "model": _engine.model.name,
        "workflows": sorted(REGISTRY),
        "runs": len(runs),
        "interrupted": sum(1 for r in runs if r.status == "interrupted"),
    }


@app.get("/workflows")
def workflows() -> dict[str, Any]:
    """Workflow names, their sample input, and their crash boundaries.

    The boundary list comes from a throwaway run on an in-memory journal with
    a recording-only crash plan -- the same mechanism the sweep uses, so the
    numbers in the UI's crash picker are the numbers the sweep tests.
    """
    from ..runtime.sweep import trace

    out = []
    for name in sorted(REGISTRY):
        reference = trace(name, SAMPLE_INPUTS[name], sink_factory=demo_sink)
        out.append(
            {
                "name": name,
                "sample_input": SAMPLE_INPUTS[name],
                "events": reference.events,
                "effects": reference.effect_keys,
                "boundaries": [
                    {"index": b.index, "hook": b.hook, "detail": b.detail}
                    for b in reference.boundaries
                ],
            }
        )
    return {"workflows": out}


@app.post("/runs")
def start_run(req: StartRequest) -> dict[str, Any]:
    if req.workflow not in REGISTRY:
        raise HTTPException(404, f"unknown workflow {req.workflow!r}")
    payload = req.input if req.input is not None else SAMPLE_INPUTS[req.workflow]
    plan = CrashPlan(at=req.crash_at) if req.crash_at else None

    try:
        if req.auto_recover:
            result, resumes = _engine.run_to_completion(
                req.workflow, payload, run_id=req.run_id, crash=plan
            )
            body = _result(result)
            body["resumes"] = resumes
            return body
        result = _engine.start(req.workflow, payload, run_id=req.run_id, crash=plan)
    except SimulatedCrash as crashed:
        # The crash is the point. Report the run as recovery would find it.
        run_id = req.run_id or _latest_interrupted()
        body = _result(_engine.status_of(run_id)) if run_id else {"status": "unknown"}
        body["crashed_at"] = str(crashed)
        body["resumes"] = 0
        return body
    except RunAlreadyTerminal as exc:
        raise HTTPException(409, str(exc)) from exc
    except RunNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    body = _result(result)
    body["resumes"] = 0
    return body


def _latest_interrupted() -> str:
    """The run a crash just created, when the caller did not name one.

    ``Engine.start`` generates the id, and a crash means it never came back to
    tell us. Reading the most recently updated interrupted run off the journal
    recovers it -- the same way a recovery sweep finds work.
    """
    for summary in _journal.runs(20):
        if summary.status == "interrupted":
            return summary.run_id
    return ""


@app.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {
        "runs": [
            {
                "run_id": s.run_id,
                "workflow": s.workflow,
                "status": s.status,
                "events": s.events,
                "created_ms": s.created_ms,
                "updated_ms": s.updated_ms,
            }
            for s in _journal.runs(limit)
        ]
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    log = _journal.read(run_id)
    if not log:
        raise HTTPException(404, f"no run {run_id!r}")
    summary = _journal.summary(run_id)
    resumes = [e.seq for e in log if e.kind == "RunResumed"]
    effects: dict[str, dict[str, Any]] = {}
    for event in log:
        payload = event.payload()
        key = payload.get("idempotency_key")
        if not key:
            continue
        row = effects.setdefault(
            key,
            {
                "key": key,
                "effect": payload.get("effect", ""),
                "attempts": 0,
                "applied": False,
                "deduplicated": False,
                "costly": payload.get("effect", "") in COSTLY_EFFECTS,
            },
        )
        if event.kind == "EffectIntended":
            row["attempts"] += 1
        elif event.kind == "EffectFailed":
            row["attempts"] += 1
        elif event.kind == "EffectObserved":
            row["applied"] = True
            row["deduplicated"] = bool(payload.get("deduplicated"))
    return {
        "run_id": run_id,
        "workflow": summary.workflow if summary else "",
        "status": summary.status if summary else "unknown",
        "events": len(log),
        "resumed_at": resumes,
        "output": (
            dict(log[-1].payload().get("output", {}))
            if log[-1].kind == "RunCompleted"
            else {}
        ),
        "timeline": [_row(e) for e in log],
        "effects": list(effects.values()),
    }


@app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def get_log(run_id: str) -> str:
    """The raw log, one canonical JSON object per line.

    This is the artefact the whole design is about, so it is downloadable
    rather than only renderable.
    """
    text = _journal.log_text(run_id)
    if not text:
        raise HTTPException(404, f"no run {run_id!r}")
    return text


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str) -> dict[str, Any]:
    try:
        return _result(_engine.resume(run_id))
    except RunNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunAlreadyTerminal as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConcurrencyError as exc:
        raise HTTPException(409, f"another worker owns this run: {exc}") from exc


@app.post("/runs/{run_id}/verify")
def verify_run(run_id: str) -> dict[str, Any]:
    try:
        report = _engine.verify_replay(run_id)
    except RunNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunAlreadyTerminal as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "run_id": report.run_id,
        "ok": report.ok,
        "deterministic": report.deterministic,
        "identical_bytes": report.identical_bytes,
        "events_checked": report.events_checked,
        "output_matches": report.output_matches,
        "detail": report.detail,
    }


@app.post("/recover")
def recover() -> dict[str, Any]:
    results = _engine.recover()
    return {
        "recovered": [_result(r) for r in results],
        "remaining": _engine.interrupted(),
    }


@app.get("/effects")
def effects() -> dict[str, Any]:
    """The sink's ledger: one row per idempotency key.

    ``applied`` is the number that matters. It can only ever be 1 per key if
    the sink is doing its half of the job, so a row reading 2 is the visible
    form of the bug this project exists to rule out.
    """
    rows = [
        {
            "key": key,
            "applied": count,
            "attempts": _sink.attempts.get(key, 0),
            "deduplicated": _sink.attempts.get(key, 0) - count,
        }
        for key, count in _sink.applied.items()
    ]
    return {
        "keys": len(rows),
        "applied_total": _sink.total_applied(),
        "duplicates": _sink.duplicates(),
        "rows": rows[-100:],
        "journal": _sink.journal[-100:],
    }


@app.post("/sweep")
def sweep(workflow: str | None = None) -> dict[str, Any]:
    """Run the exhaustive crash sweep on demand and return the results.

    It runs in roughly a tenth of a second on in-memory journals, which is why
    it can be a button. Each call is self-contained -- its own journals, its
    own sinks -- so clicking it does not touch the runs listed in the console.
    """
    names = [workflow] if workflow else sorted(REGISTRY)
    for name in names:
        if name not in REGISTRY:
            raise HTTPException(404, f"unknown workflow {name!r}")
    out = {}
    for name in names:
        res = sweep_workflow(name)
        out[name] = {
            "events": res.reference.events,
            "boundaries": len(res.reference.boundaries),
            "scenarios": res.scenarios,
            "passed": res.scenarios - len(res.failures),
            "never_started": res.never_started,
            "resumes": res.resumes,
            "dedupe_saves": res.dedupe_saves,
            "failures": [
                {
                    "boundary": r.boundary.label(),
                    "status": r.final_status,
                    "notes": r.notes,
                }
                for r in res.failures
            ],
            "hooks": sorted({r.boundary.hook for r in res.reports}),
            # One row per scenario, so the console's matrix is painted from
            # what actually happened rather than inferred from the totals.
            "rows": [
                {
                    "index": r.boundary.index,
                    "hook": r.boundary.hook,
                    "detail": r.boundary.detail,
                    "status": r.final_status,
                    "ok": r.ok or r.final_status == "never-started",
                    "resumes": r.resumes,
                    "dedupe": any("dedupe" in n for n in r.notes),
                }
                for r in res.reports
            ],
        }
    total = sum(v["scenarios"] for v in out.values())
    passed = sum(v["passed"] for v in out.values())
    return {"total": total, "passed": passed, "ok": total == passed, "workflows": out}


@app.get("/")
def console() -> FileResponse:
    index = WEB_DIR / "index.html"
    if not index.exists():  # pragma: no cover - deploy sanity
        raise HTTPException(404, "console not built")
    return FileResponse(index)
