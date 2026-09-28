"""Local web console for the prototype: run scenarios, act as the SRE, inspect audit and metrics.

    python -m autoheal ui            → http://127.0.0.1:8080

Same graph, checkpointer, MCP server and audit log as the CLI, so an incident paused in the
browser can be approved from the CLI and vice versa. Binds to localhost only.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.types import Command
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import agents
from .audit import AUDIT
from .graph import _cfg, ensure_cloud_for, new_incident, open_app
from .infra_sim import CLOUD, list_scenarios, load_scenario
from .llm import ROUTER, LiteLLMBackend, MockLLM
from .telemetry import METRICS, span

STATIC = Path(__file__).parent / "static" / "index.html"
GRAPH: dict[str, Any] = {}          # the compiled graph, opened in lifespan
TASKS: dict[str, asyncio.Task] = {}  # incident id → running graph task
ERRORS: dict[str, str] = {}
ORDER: list[str] = []               # incidents started from this console, newest last


@asynccontextmanager
async def lifespan(app):
    agents.set_emitter(lambda msg: print(msg, flush=True))
    async with open_app() as graph:
        GRAPH["app"] = graph
        yield


def _mode() -> str:
    return "openai" if isinstance(ROUTER.backend, LiteLLMBackend) else "mock"


def _run_in_background(inc_id: str, coro) -> None:
    async def runner():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 (surfaced to the UI instead of crashing the server)
            ERRORS[inc_id] = f"{type(exc).__name__}: {exc}"
    ERRORS.pop(inc_id, None)
    TASKS[inc_id] = asyncio.create_task(runner())


async def _snapshot(inc_id: str) -> dict[str, Any]:
    snap = await GRAPH["app"].aget_state(_cfg(inc_id))
    v = snap.values or {}
    task = TASKS.get(inc_id)
    running = task is not None and not task.done()
    pending = snap.interrupts[0].value if snap.interrupts and not running else None
    status = "running" if running else ("awaiting_approval" if pending else v.get("status", "unknown"))
    if inc_id in ERRORS:
        status = "error"
    inc = v.get("incident", {})
    return {"incident_id": inc_id, "status": status, "scenario": inc.get("scenario"),
            "target": f"{inc.get('cluster')}/{inc.get('namespace')}/{inc.get('service')}" if inc else None,
            "timeline": v.get("timeline", []), "approval_request": pending, "error": ERRORS.get(inc_id),
            "risk": v.get("risk"), "rca": v.get("rca"), "tokens_used": v.get("tokens_used", 0),
            "exec_result": v.get("exec_result")}


# ------------------------------------------------------------------------------ API
async def index(request: Request):
    return HTMLResponse(STATIC.read_text(encoding="utf-8"))


async def api_config(request: Request):
    return JSONResponse({"mode": _mode(), "openai_available": bool(os.getenv("OPENAI_API_KEY")),
                         "scenarios": [{"name": n, "description": load_scenario(n)["description"],
                                        "severity": load_scenario(n)["alert"]["severity"]} for n in list_scenarios()]})


async def api_mode(request: Request):
    body = await request.json()
    if body.get("mode") == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            return JSONResponse({"error": "OPENAI_API_KEY is not set in the terminal that started the UI"}, 400)
        ROUTER.backend = LiteLLMBackend()
    else:
        ROUTER.backend = MockLLM()
    ROUTER.cache.clear()   # never serve a mock answer to a real run or vice versa
    return JSONResponse({"mode": _mode()})


async def api_run(request: Request):
    body = await request.json()
    name = body.get("scenario")
    if name not in list_scenarios():
        return JSONResponse({"error": f"unknown scenario {name!r}"}, 400)
    if any(not t.done() for t in TASKS.values()):
        return JSONResponse({"error": "another incident is still running; wait for it to pause or finish"}, 409)
    CLOUD.seed(load_scenario(name))
    state = new_incident(name)
    inc_id = state["incident"]["id"]
    ORDER.append(inc_id)

    async def go():
        with span("incident", **{"incident.id": inc_id, "scenario": name, "ui": True}):
            await GRAPH["app"].ainvoke(state, _cfg(inc_id))
    _run_in_background(inc_id, go())
    await asyncio.sleep(0.05)
    return JSONResponse(await _snapshot(inc_id))


async def api_decide(request: Request):
    inc_id = request.path_params["incident_id"]
    body = await request.json()
    kind = body.get("decision")
    if kind not in ("approve", "reject", "modify"):
        return JSONResponse({"error": "decision must be approve, reject or modify"}, 400)
    snap = await _snapshot(inc_id)
    if snap["status"] != "awaiting_approval":
        return JSONResponse({"error": f"{inc_id} is not waiting for approval (status: {snap['status']})"}, 409)
    decision = {"decision": kind, "approver": (body.get("approver") or "sre.ui").strip()[:64],
                "comment": (body.get("comment") or "").strip()[:500]}
    if kind == "modify":
        decision["changes"] = {"memory_limit": (body.get("memory") or "2Gi").strip()}
    await ensure_cloud_for(GRAPH["app"], inc_id)

    async def go():
        with span("incident.resume", **{"incident.id": inc_id, "hitl.decision": kind, "ui": True}):
            await GRAPH["app"].ainvoke(Command(resume=decision), _cfg(inc_id))
    _run_in_background(inc_id, go())
    await asyncio.sleep(0.05)
    return JSONResponse(await _snapshot(inc_id))


async def api_incident(request: Request):
    try:
        return JSONResponse(await _snapshot(request.path_params["incident_id"]))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, 404)


async def api_incidents(request: Request):
    return JSONResponse([await _snapshot(i) for i in reversed(ORDER[-20:])])


async def api_audit(request: Request):
    ok, n, msg = AUDIT.verify_chain()
    rows = AUDIT.entries(request.path_params["incident_id"])
    return JSONResponse({"chain_valid": ok, "chain_records": n, "chain_message": msg,
                         "entries": [{"ts": r["ts"], "actor": r["actor"], "event": r["event"],
                                      "details": r["details"], "hash": r["hash"][:12]} for r in rows]})


async def api_metrics(request: Request):
    return JSONResponse({"mode": _mode(), **METRICS.summary(), "server_time": time.time()})


app = Starlette(lifespan=lifespan, routes=[
    Route("/", index),
    Route("/api/config", api_config),
    Route("/api/mode", api_mode, methods=["POST"]),
    Route("/api/run", api_run, methods=["POST"]),
    Route("/api/incidents", api_incidents),
    Route("/api/incidents/{incident_id}", api_incident),
    Route("/api/incidents/{incident_id}/decision", api_decide, methods=["POST"]),
    Route("/api/incidents/{incident_id}/audit", api_audit),
    Route("/api/metrics", api_metrics),
])


def serve(port: int = 8080, open_browser: bool = True) -> None:
    import uvicorn
    import webbrowser

    url = f"http://127.0.0.1:{port}"
    print(f"CloudScale AIOps console → {url}   (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
