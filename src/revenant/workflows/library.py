"""Three workflows, chosen because they fail differently.

The fault sweep is only as good as the shapes it runs over, so these are not
three variations on one agent:

``research``
    Model-heavy, two effects, a clock read. The ordinary agent shape.
``onboarding``
    Four effects back to back and no model at all. This is where duplicate
    effects would show up as a double charge, so it is the workflow that
    makes the exactly-once claim concrete rather than abstract.
``triage``
    Branches on a journaled random draw and only acts on some items. A crash
    here is nastier than it looks: if the draw were not journaled, the resumed
    run would take the *other* branch and either skip an effect it had already
    published or publish one it had not. The branch is a decision; the draw is
    an observation; keeping those straight is the whole point.

All three are plain functions. Nothing is decorated, nothing is a generator,
and the durability comes entirely from going through ``ctx``.
"""

from __future__ import annotations

from typing import Any

from ..runtime.context import RunContext
from ..runtime.effects import LedgerSink


def research(ctx: RunContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Plan -> gather -> synthesise -> publish."""
    topic = payload.get("topic", "untitled")
    depth = int(payload.get("depth", 2))

    with ctx.step("plan"):
        plan = ctx.call_model(f"Break the topic into {depth} search queries: {topic}")
        ctx.state["plan"] = plan
        # Derived from the recorded completion, so it replays identically.
        ctx.state["queries"] = [f"{topic} :: {plan[:12]} #{i}" for i in range(depth)]

    with ctx.step("gather"):
        notes = []
        for query in ctx.state["queries"]:
            hit = ctx.effect("search", {"query": query})
            notes.append(hit.get("snippet", ""))
        ctx.state["notes"] = notes

    with ctx.step("synthesise"):
        summary = ctx.call_model(
            "Summarise these notes for a reader who knows the field:\n"
            + "\n".join(ctx.state["notes"])
        )
        ctx.state["summary"] = summary

    with ctx.step("publish"):
        receipt = ctx.effect(
            "publish_report",
            {"topic": topic, "summary": ctx.state["summary"]},
        )
        published_at = ctx.now_ms()

    return {
        "topic": topic,
        "summary": ctx.state["summary"],
        "sources": len(ctx.state["notes"]),
        "report_id": receipt.get("report_id", ""),
        "published_at": published_at,
    }


def onboarding(ctx: RunContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Create, charge, provision, notify -- four effects, no model.

    Written in the order a real signup has to run: the charge is before the
    provisioning so a crash between them leaves a paid-for account that
    recovery finishes, rather than a provisioned account nobody paid for.
    """
    email = payload.get("email", "nobody@example.com")
    plan = payload.get("plan", "free")
    cents = int(payload.get("cents", 900))

    with ctx.step("create-account"):
        account = ctx.effect("create_account", {"email": email, "plan": plan})
        ctx.state["account_id"] = account["account_id"]

    with ctx.step("charge"):
        charge = ctx.effect(
            "charge_card",
            {"account_id": ctx.state["account_id"], "cents": cents},
        )
        ctx.state["charge_id"] = charge["charge_id"]

    with ctx.step("provision"):
        workspace = ctx.effect(
            "provision_workspace",
            {"account_id": ctx.state["account_id"], "plan": plan},
        )
        ctx.state["workspace"] = workspace["workspace"]

    with ctx.step("notify"):
        ctx.effect(
            "send_email",
            {
                "to": email,
                "template": "welcome",
                "workspace": ctx.state["workspace"],
            },
        )

    return {
        "account_id": ctx.state["account_id"],
        "charge_id": ctx.state["charge_id"],
        "workspace": ctx.state["workspace"],
        "cents": cents,
    }


def triage(ctx: RunContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Score each ticket, and page someone only for the severe ones."""
    tickets = list(payload.get("tickets", ["disk full", "login loop", "slow query"]))
    threshold = int(payload.get("threshold", 70))

    scored: list[dict[str, Any]] = []
    with ctx.step("score"):
        for ticket in tickets:
            # The draw is journaled; the comparison against the threshold is
            # not, because it is a pure function of the draw.
            severity = ctx.draw(100)
            label = ctx.call_model(f"One-line severity rationale for: {ticket}")
            scored.append(
                {"ticket": ticket, "severity": severity, "rationale": label}
            )
        ctx.state["scored"] = scored

    paged = []
    with ctx.step("page"):
        for item in scored:
            if item["severity"] < threshold:
                continue
            ack = ctx.effect(
                "page_oncall",
                {"ticket": item["ticket"], "severity": item["severity"]},
            )
            paged.append(ack["page_id"])
        ctx.state["paged"] = paged

    return {
        "triaged": len(scored),
        "paged": paged,
        "max_severity": max((i["severity"] for i in scored), default=0),
    }


REGISTRY: dict[str, Any] = {
    "research": research,
    "onboarding": onboarding,
    "triage": triage,
}

#: Inputs the API and the sweep use when none is supplied, so a demo click and
#: a test run exercise the same thing.
SAMPLE_INPUTS: dict[str, dict[str, Any]] = {
    "research": {"topic": "conformal prediction for load forecasting", "depth": 2},
    "onboarding": {"email": "madhura@example.com", "plan": "pro", "cents": 1900},
    "triage": {
        "tickets": ["disk full on shard 3", "login loop after SSO", "slow query p99"],
        "threshold": 60,
    },
}


def demo_sink() -> LedgerSink:
    """A sink whose handlers are pure functions of their arguments.

    Deliberately pure: the sweep compares the final output of a crashed-and-
    recovered run against the clean run's output, and a handler that returned
    a timestamp or a uuid would make those differ for a reason that has
    nothing to do with durability. Real sinks are not pure, which is why the
    runtime records their results instead of recomputing them -- but for the
    oracle to be sharp, these are.
    """
    from ..store.base import digest

    def search(args: dict[str, Any]) -> dict[str, Any]:
        q = args["query"]
        return {"snippet": f"[{digest(q)}] evidence for {q[:40]}", "rank": len(q) % 5}

    def publish_report(args: dict[str, Any]) -> dict[str, Any]:
        return {"report_id": "rep_" + digest(args), "url": f"/reports/{digest(args)}"}

    def create_account(args: dict[str, Any]) -> dict[str, Any]:
        return {"account_id": "acct_" + digest(args["email"])}

    def charge_card(args: dict[str, Any]) -> dict[str, Any]:
        return {"charge_id": "ch_" + digest(args), "cents": args["cents"]}

    def provision_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return {"workspace": "ws_" + digest(args)}

    def send_email(args: dict[str, Any]) -> dict[str, Any]:
        return {"message_id": "msg_" + digest(args)}

    def page_oncall(args: dict[str, Any]) -> dict[str, Any]:
        return {"page_id": "pg_" + digest(args), "acked": True}

    return LedgerSink(
        handlers={
            "search": search,
            "publish_report": publish_report,
            "create_account": create_account,
            "charge_card": charge_card,
            "provision_workspace": provision_workspace,
            "send_email": send_email,
            "page_oncall": page_oncall,
        }
    )


#: Effects that spend money or reach a human. The dashboard highlights these,
#: because "did this run exactly once" is only interesting for the ones where
#: twice would be a real problem.
COSTLY_EFFECTS = frozenset({"charge_card", "send_email", "page_oncall"})
