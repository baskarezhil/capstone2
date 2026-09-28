"""LangGraph wiring (ADR 01). The only edge into `executor` comes from `hitl_gate`."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from . import agents as A
from .config import SETTINGS
from .infra_sim import CLOUD, load_scenario
from .models import IncidentState
from .telemetry import span


def build_graph(checkpointer=None):
    g = StateGraph(IncidentState)
    for name in ("intake", "supervisor", "triage", "planner", "risk_gate", "hitl_request", "hitl_gate",
                 "executor", "verifier", "escalate", "close"):
        g.add_node(name, getattr(A, name))

    g.add_edge(START, "intake")
    g.add_edge("intake", "supervisor")
    g.add_conditional_edges("supervisor", A.route_from_supervisor,
                            ["triage", "planner", "risk_gate", "escalate", "close"])
    g.add_edge("triage", "supervisor")
    g.add_edge("planner", "supervisor")
    g.add_conditional_edges("risk_gate", A.route_from_risk_gate, ["supervisor", "hitl_request", "hitl_gate"])
    g.add_edge("hitl_request", "hitl_gate")
    g.add_conditional_edges("hitl_gate", A.route_from_hitl, ["executor", "supervisor"])
    g.add_conditional_edges("executor", A.route_from_executor, ["verifier", "supervisor"])
    g.add_edge("verifier", "supervisor")
    g.add_edge("escalate", END)
    g.add_edge("close", END)
    return g.compile(checkpointer=checkpointer)


@asynccontextmanager
async def open_app() -> AsyncIterator[Any]:
    """Graph + durable SQLite checkpointer (production: Redis/Postgres checkpointer)."""
    async with AsyncSqliteSaver.from_conn_string(str(SETTINGS.checkpoint_db)) as saver:
        yield build_graph(saver)


def new_incident(scenario_name: str) -> IncidentState:
    sc = load_scenario(scenario_name)
    lab = sc["alert"]["labels"]
    inc_id = f"INC-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    return {"incident": {"id": inc_id, "scenario": scenario_name, "alert": sc["alert"],
                         "cluster": lab["cluster"], "namespace": lab["namespace"], "service": lab["service"],
                         "opened_at": time.time(),
                         "simulate_compromised_planner": sc.get("simulate_compromised_planner", False)},
            "timeline": []}


def _cfg(incident_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": incident_id}, "recursion_limit": SETTINGS.recursion_limit}


async def start_incident(app, scenario_name: str) -> tuple[str, dict[str, Any]]:
    CLOUD.seed(load_scenario(scenario_name))
    state = new_incident(scenario_name)
    inc_id = state["incident"]["id"]
    with span("incident", **{"incident.id": inc_id, "scenario": scenario_name}):
        result = await app.ainvoke(state, _cfg(inc_id))
    return inc_id, result


async def resume_incident(app, incident_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    with span("incident.resume", **{"incident.id": incident_id, "hitl.decision": decision.get("decision")}):
        return await app.ainvoke(Command(resume=decision), _cfg(incident_id))


async def pending_approval(app, incident_id: str) -> dict[str, Any] | None:
    snap = await app.aget_state(_cfg(incident_id))
    return snap.interrupts[0].value if snap.interrupts else None


async def ensure_cloud_for(app, incident_id: str) -> None:
    """When resuming in a new process, re-hydrate the simulated cluster for that scenario."""
    snap = await app.aget_state(_cfg(incident_id))
    if not snap.values:
        raise LookupError(f"no checkpoint for {incident_id}")
    key = (snap.values["incident"]["cluster"], snap.values["incident"]["namespace"],
           snap.values["incident"]["service"])
    if key not in CLOUD.services:
        sc = load_scenario(snap.values["incident"]["scenario"])
        CLOUD.seed(sc)
        CLOUD.transient_failures_left = 0
