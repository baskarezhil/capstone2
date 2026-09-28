"""Agent nodes for the hierarchical incident graph (ADR 01).

Supervisor ─┬─> Triage & Diagnosis     (READ tools only)
            ├─> Remediation Planner    (no tools)
            └─> Risk gate ─> HITL gate ─> Execution & Verification (only write-capable identity)
"""
from __future__ import annotations

import functools
import json
import time
from typing import Any, Awaitable, Callable

from fastmcp.exceptions import ToolError
from langgraph.types import interrupt

from .audit import AUDIT
from .config import SETTINGS
from .guardrails import CommandGuardrail, InputGuardrail, OutputGuardrail
from .hitl import build_approval_request, issue_token
from .llm import ROUTER
from .mcp_client import MCPGateway
from .models import IncidentState, RiskClass, Status, patch_digest
from .resilience import CircuitOpenError, TransientError
from .risk import classify, requires_human  # noqa: F401
from .telemetry import METRICS, span

guard_in, guard_cmd, guard_out = InputGuardrail(), CommandGuardrail(), OutputGuardrail()

_emit: Callable[[str], None] = print


def set_emitter(fn: Callable[[str], None]) -> None:
    global _emit
    _emit = fn


def say(msg: str) -> list[str]:
    _emit(msg)
    return [msg]


def _target(inc: dict[str, Any]) -> dict[str, str]:
    return {"cluster": inc["cluster"], "namespace": inc["namespace"], "deployment": inc["service"]}


def _scope(inc: dict[str, Any]) -> tuple:
    """Cache key scope: never reuse an answer across clusters/services/rules (ADR 03)."""
    return (inc["cluster"], inc["namespace"], inc["service"], inc["alert"]["rule"])


def agent_node(name: str):
    """Span + latency metric + failure containment for every node."""
    def deco(fn: Callable[[IncidentState], Awaitable[dict[str, Any]]]):
        @functools.wraps(fn)
        async def wrapper(state: IncidentState) -> dict[str, Any]:
            inc = state["incident"]
            with span(f"agent.{name}", **{"incident.id": inc["id"], "status.in": state.get("status")}):
                with METRICS.timer(f"task.{name}"):
                    try:
                        return await fn(state)
                    except (ToolError, TransientError, CircuitOpenError) as exc:
                        AUDIT.record(inc["id"], name, "node.failed", error=str(exc))
                        return {"status": Status.ESCALATE.value,
                                "exec_result": {"error": str(exc)},
                                "timeline": say(f"   ✖ {name} failed: {exc}")}
        return wrapper
    return deco


# ------------------------------------------------------------------------------ prompts
CONF = "confidence is a number from 0 to 1 (how sure you are); use a low value when evidence is weak"
PROMPT_CLASSIFY = ("Classify this monitoring alert. Return JSON {{\"category\": one of memory_exhaustion | "
                   "stale_cache | error_spike | unknown, \"severity\": \"P1\"-\"P4\", \"confidence\"}}; " + CONF + ".\n{alert}")
PROMPT_SUMMARY = ("Summarise the log evidence. Return JSON {{\"summary\": string, \"signatures\": [up to 5 "
                  "distinct error patterns], \"confidence\"}}; " + CONF + ".\n{logs}")
PROMPT_RCA = ("You are the Triage & Diagnosis agent. Using the alert, health and log evidence, determine the root "
              "cause. Return JSON {{\"category\": one of memory_exhaustion | stale_cache | error_spike | unknown, "
              "\"root_cause\": one or two sentences, \"confidence\", \"evidence\": [short strings]}}; " + CONF + ".\n"
              "ALERT: {alert}\nHEALTH: {health}\n{logs}")
PROMPT_PLAN = ("You are the Remediation Planner. Propose exactly ONE action for the target below, using only:\n"
               "- restart_service: zero-downtime rolling restart of the Deployment (clears in-memory / pod caches)\n"
               "- apply_hotfix: a Kubernetes strategic-merge patch to the target Deployment (for example resource "
               "limits). The container name equals the deployment name.\n"
               "Never propose shell commands, kubectl exec, scripts, downloads, privileged settings, host mounts or "
               "any other target. If no safe action exists, return {{\"tool\": null, \"rationale\": \"why\"}}.\n"
               "Apply any SRE OVERRIDES exactly (for example memory_limit).\n"
               "TARGET: {incident}\nRCA: {rca}\nPREVIOUS GUARDRAIL FEEDBACK: {feedback}\nSRE OVERRIDES: {overrides}\n"
               "Return JSON in exactly this shape (values are an example):\n")
PLAN_EXAMPLE = json.dumps({
    "tool": "apply_hotfix", "intent": "config_patch",
    "args": {"patch": {"spec": {"template": {"spec": {"containers": [
        {"name": "<deployment>", "resources": {"limits": {"memory": "1Gi"}, "requests": {"memory": "768Mi"}}}]}}}}},
    "command_preview": "kubectl patch deployment <deployment> -n <namespace> --type=strategic -p '<patch json>'",
    "rationale": "why this fixes the root cause", "diff": "- limits.memory: 512Mi\n+ limits.memory: 1Gi",
    "rollback": "kubectl rollout undo deployment/<deployment> -n <namespace>",
    "blast_radius": "which workloads and replicas are affected"})
PROMPT_VERIFY = ("Is the service healthy after remediation? It is healthy only if status is \"healthy\" and "
                 "error_rate < 0.01. Return JSON {{\"healthy\": true|false, \"reason\", \"confidence\"}}.\n{health}")
PLANNABLE_TOOLS = {"restart_service", "apply_hotfix"}


def normalize_plan(plan: dict[str, Any], target: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Pin the plan to the incident's own target and the MCP argument shape. A model can choose the
    action, never where it runs. Returns (plan, notes about anything that was overridden)."""
    notes: list[str] = []
    args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
    for key, value in target.items():
        if args.get(key) not in (None, value):
            notes.append(f"model targeted {key}={args.get(key)!r}; pinned to {value!r}")
    if plan.get("tool") == "apply_hotfix":
        raw = args.get("patch") if isinstance(args.get("patch"), dict) else {}
        body = raw.get("patch") if isinstance(raw.get("patch"), dict) else raw
        args = {**target, "patch": {"target": target, "type": "strategic_merge", "patch": body}}
    else:
        args = dict(target)
    plan["args"] = args
    plan["command_preview"] = str(plan.get("command_preview") or f"{plan.get('tool')} {target}")
    return plan, notes


# ------------------------------------------------------------------------------ supervisor
@agent_node("supervisor.intake")
async def intake(state: IncidentState) -> dict[str, Any]:
    inc = state["incident"]
    alert = inc["alert"]
    g = guard_in.sanitize(alert["description"])
    cls, tokens = ROUTER.complete("classify", PROMPT_CLASSIFY.format(alert=g.text), {"alert": alert},
                                  cache_scope=_scope(inc), cache_text=f"{alert['rule']} {g.text}")
    AUDIT.record(inc["id"], "supervisor", "incident.opened", rule=alert["rule"], severity=alert.get("severity"),
                 target=f"{inc['cluster']}/{inc['namespace']}/{inc['service']}", classification=cls)
    return {"status": Status.NEW.value, "attempts": 0, "tokens_used": tokens, "guardrail_feedback": [],
            "timeline": say(f"🟣 Supervisor: opened {inc['id']} [{alert.get('severity')}] {alert['summary']}"
                            f" → category={cls['category']} (conf {cls['confidence']})"
                            + (" [cache]" if cls.get("_cached") else ""))}


@agent_node("supervisor")
async def supervisor(state: IncidentState) -> dict[str, Any]:
    """Owns budgets and limits; routing itself happens in `route_from_supervisor`."""
    inc = state["incident"]
    if state.get("tokens_used", 0) > SETTINGS.token_budget_per_incident:
        AUDIT.record(inc["id"], "supervisor", "budget.exceeded", tokens=state["tokens_used"])
        return {"status": Status.ESCALATE.value, "timeline": say("🟣 Supervisor: token budget exceeded → escalate")}
    if state.get("attempts", 0) > SETTINGS.max_remediation_attempts:
        return {"status": Status.ESCALATE.value,
                "timeline": say(f"🟣 Supervisor: {state['attempts']} failed attempts > limit "
                                f"{SETTINGS.max_remediation_attempts} → circuit open, escalate")}
    return {}


def route_from_supervisor(state: IncidentState) -> str:
    s = state.get("status")
    if s == Status.NEW.value:
        return "triage"
    if s == Status.DIAGNOSED.value:
        return "planner" if state["rca"].get("confidence", 0) >= SETTINGS.rca_min_confidence else "escalate"
    if s in (Status.BLOCKED.value, Status.VERIFY_FAILED.value, "replan"):
        return "planner"
    if s == Status.PLANNED.value:
        return "risk_gate"
    if s == Status.RESOLVED.value:
        return "close"
    return "escalate"  # rejected, escalate, anything unexpected → humans (fail closed)


# ------------------------------------------------------------------------------ workers
@agent_node("triage")
async def triage(state: IncidentState) -> dict[str, Any]:
    inc = state["incident"]
    gw = MCPGateway("triage-agent", inc["id"], on_event=_emit)
    logs = await gw.call("fetch_k8s_logs", **_target(inc), since_minutes=30, tail=200)
    health = await gw.call("get_service_health", **_target(inc))

    g = guard_in.sanitize(logs["logs"])
    if g.injections:
        METRICS.inc("guardrail.injections", len(g.injections))
        AUDIT.record(inc["id"], "input-guardrail", "guardrail.injection_quarantined", lines=g.injections)
    wrapped = guard_in.wrap_untrusted(g.text, "k8s_logs")
    scope = _scope(inc)
    summ, t1 = ROUTER.complete("summarize_logs", PROMPT_SUMMARY.format(logs=wrapped), {"logs": g.text},
                               cache_scope=scope, cache_text=g.text)
    rca, t2 = ROUTER.complete("rca", PROMPT_RCA.format(alert=json.dumps(inc["alert"]), health=json.dumps(health),
                                                       logs=wrapped),
                              {"logs": g.text, "health": health, "alert": inc["alert"]},
                              cache_scope=scope, cache_text=g.text)
    rca = guard_out.scrub(rca)   # L4: model output never carries secrets downstream
    AUDIT.record(inc["id"], "triage-agent", "rca.produced", category=rca["category"],
                 confidence=rca["confidence"], cached=bool(rca.get("_cached")))
    msgs = [f"🔵 Triage: fetch_k8s_logs ({logs['line_count']} lines, redacted={logs['redactions'] or 'none'}) "
            f"+ health={health['status']} err={health['error_rate']:.1%}"]
    if g.injections:
        msgs.append(f"   🛡 Input guardrail quarantined {len(g.injections)} prompt-injection line(s): "
                    f"\"{g.injections[0][:70]}…\"")
    msgs.append(f"🔵 Triage RCA ({rca['category']}, conf {rca['confidence']})"
                f"{' [semantic cache hit]' if rca.get('_cached') else ''}: {rca['root_cause']}")
    return {"status": Status.DIAGNOSED.value, "rca": {k: v for k, v in rca.items() if k != "_cached"},
            "evidence": {"health": health, "summary": summ, "injections": g.injections,
                         "redactions": logs["redactions"]},
            "tokens_used": t1 + t2, "timeline": [m for msg in msgs for m in say(msg)]}


@agent_node("planner")
async def planner(state: IncidentState) -> dict[str, Any]:
    inc, rca = state["incident"], state["rca"]
    overrides = inc.get("overrides", {})
    payload = {"incident": inc, "rca": rca, "attempt": state.get("attempts", 0),
               "feedback": state.get("guardrail_feedback", []),
               "compromised": inc.get("simulate_compromised_planner", False), **overrides}
    prompt = PROMPT_PLAN.format(incident=json.dumps(_target(inc)), rca=json.dumps(rca),
                                feedback=payload["feedback"], overrides=overrides) + PLAN_EXAMPLE
    plan, tokens = ROUTER.complete("plan", prompt, payload)  # plans are never cached (ADR 03)
    if not plan.get("tool"):
        return {"status": Status.ESCALATE.value, "plan": None, "tokens_used": tokens,
                "timeline": say(f"🔵 Planner: {plan.get('rationale')}")}
    plan, pin_notes = normalize_plan(plan, _target(inc))
    if pin_notes:
        AUDIT.record(inc["id"], "planner-agent", "plan.retargeted", notes=pin_notes)
    # L4: scrub free-text fields; args stay untouched (validated by L3 and digested for approval)
    plan = {k: (v if k == "args" else guard_out.scrub(v)) for k, v in plan.items()}
    if plan["tool"] == "apply_hotfix":
        plan["args"]["patch_id"] = patch_digest(plan["args"]["patch"])
    AUDIT.record(inc["id"], "planner-agent", "plan.proposed", tool=plan["tool"], intent=plan.get("intent"),
                 command_preview=plan.get("command_preview"), patch_id=plan["args"].get("patch_id"))
    return {"status": Status.PLANNED.value, "plan": plan, "tokens_used": tokens,
            "timeline": say(f"🔵 Planner (attempt {state.get('attempts', 0) + 1}): {plan['tool']} → "
                            f"`{plan.get('command_preview', '')[:110]}`")}


@agent_node("risk_gate")
async def risk_gate(state: IncidentState) -> dict[str, Any]:
    """Deterministic L3 guardrail + risk classification. Not an LLM."""
    inc, plan = state["incident"], state["plan"]
    verdict = guard_cmd.check_action(plan)
    if not verdict.allowed:
        METRICS.inc("guardrail.blocks")
        AUDIT.record(inc["id"], "command-guardrail", "guardrail.blocked", violations=verdict.violations,
                     command_preview=plan.get("command_preview"))
        return {"status": Status.BLOCKED.value, "plan": None, "attempts": state.get("attempts", 0) + 1,
                "guardrail_feedback": state.get("guardrail_feedback", []) + verdict.violations,
                "timeline": say(f"   🛡 Command guardrail BLOCKED plan: {', '.join(verdict.violations)} → re-plan")}
    risk = classify(plan)
    conf = state["rca"].get("confidence", 0)
    if requires_human(risk):
        reason = f"{risk.value} action"
    elif conf < SETTINGS.hitl_confidence_threshold:
        reason = f"RCA confidence C={conf} < {SETTINGS.hitl_confidence_threshold}"
    else:
        reason = None
    AUDIT.record(inc["id"], "risk-classifier", "risk.classified", tool=plan["tool"], risk=risk.value,
                 rca_confidence=conf, hitl_reason=reason)
    return {"risk": risk.value, "needs_human": reason is not None, "hitl_reason": reason,
            "timeline": say(f"🔴 Risk classifier: {plan['tool']} = {risk.value}, C={conf}"
                            + (f" → human required ({reason})" if reason else ""))}


def route_from_risk_gate(state: IncidentState) -> str:
    if state["status"] == Status.BLOCKED.value:
        return "supervisor"
    return "hitl_request" if state.get("needs_human") else "hitl_gate"


@agent_node("hitl_request")
async def hitl_request(state: IncidentState) -> dict[str, Any]:
    """Runs once per approval; kept separate because `interrupt()` re-executes its node on resume."""
    inc = state["incident"]
    req = build_approval_request(state)
    METRICS.inc("hitl.requests")
    AUDIT.record(inc["id"], "hitl-gate", "hitl.requested", patch_id=req["proposed_action"]["patch_id"],
                 risk=req["risk"], reason=req["hitl_reason"], notify=["slack:#sre-oncall", "teams:SRE"])
    return {"approval": {"requested_at": time.time()},
            "timeline": say(f"⏸  HITL gate: {state['hitl_reason']} → graph checkpointed and PAUSED; approval card sent to SRE")}


@agent_node("hitl_gate")
async def hitl_gate(state: IncidentState) -> dict[str, Any]:
    inc, plan, risk = state["incident"], state["plan"], state["risk"]
    if not state.get("needs_human"):
        AUDIT.record(inc["id"], "hitl-gate", "hitl.auto_approved", risk=risk, tool=plan["tool"])
        return {"status": Status.APPROVED.value, "approval": {"decision": "auto", "approver": "policy:auto-pass"},
                "timeline": say(f"🟢 HITL gate: {risk} and C ≥ {SETTINGS.hitl_confidence_threshold} → auto-pass (no human needed)")}

    decision: dict[str, Any] = interrupt(build_approval_request(state))   # ⏸ pause / ▶ resume here

    d, approver = decision.get("decision"), decision.get("approver", "unknown")
    waited = round(time.time() - state.get("approval", {}).get("requested_at", time.time()), 1)
    METRICS.observe("task.hitl_wait", waited)
    base = {"decision": d, "approver": approver, "comment": decision.get("comment"), "waited_s": waited}
    if d == "approve":
        action_id = plan["args"].get("patch_id") or patch_digest(plan["args"])
        token = issue_token(inc["id"], action_id, approver)
        AUDIT.record(inc["id"], approver, "hitl.approved", patch_id=action_id,
                     comment=decision.get("comment"), waited_s=waited)
        return {"status": Status.APPROVED.value, "approval": {**base, "token": token},
                "timeline": say(f"▶  HITL gate: APPROVED by {approver} after {waited}s → resuming from checkpoint")}
    if d == "modify":
        changes = decision.get("changes", {})
        AUDIT.record(inc["id"], approver, "hitl.modified", changes=changes)
        return {"status": "replan", "plan": None, "approval": base,
                "incident": {**inc, "overrides": {**inc.get("overrides", {}), **changes}},
                "timeline": say(f"✏  HITL gate: {approver} requested changes {changes} → re-plan & re-approve")}
    AUDIT.record(inc["id"], approver, "hitl.rejected", comment=decision.get("comment"))
    return {"status": Status.REJECTED.value, "approval": base,
            "timeline": say(f"⛔ HITL gate: REJECTED by {approver}: {decision.get('comment', '')}")}


def route_from_hitl(state: IncidentState) -> str:
    return "executor" if state["status"] == Status.APPROVED.value else "supervisor"


@agent_node("executor")
async def executor(state: IncidentState) -> dict[str, Any]:
    inc, plan, approval = state["incident"], state["plan"], state["approval"]
    gw = MCPGateway("executor-agent", inc["id"], on_event=_emit)
    args = dict(plan["args"])
    if plan["tool"] == "apply_hotfix":
        args["approval_token"] = approval.get("token")
    result = await gw.call(plan["tool"], **args)
    return {"status": Status.EXECUTED.value, "exec_result": result,
            "approval": {**approval, "token": "[consumed]"},
            "timeline": say(f"🔵 Executor: {plan['tool']} via MCP → {result}")}


def route_from_executor(state: IncidentState) -> str:
    return "verifier" if state["status"] == Status.EXECUTED.value else "supervisor"


@agent_node("verifier")
async def verifier(state: IncidentState) -> dict[str, Any]:
    inc = state["incident"]
    gw = MCPGateway("executor-agent", inc["id"], on_event=_emit)
    health = await gw.call("get_service_health", **_target(inc))
    v, tokens = ROUTER.complete("verify", PROMPT_VERIFY.format(health=json.dumps(health)), {"health": health})
    # The SLO check is deterministic; the model only explains. (First real gpt-4o-mini run judged
    # "0.002 > 0.01" and would have re-restarted a healthy service.) Disagreements are audited.
    slo_ok = health.get("status") == "healthy" and float(health.get("error_rate", 1)) < 0.01
    if v.get("healthy") is not slo_ok:
        AUDIT.record(inc["id"], "executor-agent", "verification.model_disagreed",
                     model_said=v.get("healthy"), slo_check=slo_ok, reason=v.get("reason"))
    v = {**v, "healthy": slo_ok, "model_healthy": v.get("healthy"),
         "reason": f"status={health.get('status')} error_rate={health.get('error_rate')}"}
    AUDIT.record(inc["id"], "executor-agent", "verification", healthy=slo_ok, health=health)
    if v["healthy"]:
        return {"status": Status.RESOLVED.value, "verification": v, "tokens_used": tokens,
                "timeline": say(f"✅ Verifier: healthy ({v['reason']})")}
    return {"status": Status.VERIFY_FAILED.value, "verification": v, "tokens_used": tokens,
            "attempts": state.get("attempts", 0) + 1,
            "timeline": say(f"❌ Verifier: still degraded ({v['reason']}) → back to Supervisor")}


# ------------------------------------------------------------------------------ terminal nodes
@agent_node("escalate")
async def escalate(state: IncidentState) -> dict[str, Any]:
    inc = state["incident"]
    status = state.get("status")
    if status == Status.DIAGNOSED.value:
        reason = (f"RCA confidence {state['rca'].get('confidence')} below {SETTINGS.rca_min_confidence}, "
                  "no safe automated action")
    elif status == Status.REJECTED.value:
        reason = "SRE rejected the proposed action; manual remediation"
    elif state.get("attempts", 0) > SETTINGS.max_remediation_attempts:
        reason = f"{state['attempts']} failed remediation attempts (circuit open)"
    else:
        reason = (state.get("exec_result") or {}).get("error") or status
    AUDIT.record(inc["id"], "supervisor", "incident.escalated", reason=reason,
                 guardrail_feedback=state.get("guardrail_feedback"), page="pagerduty:cloudscale-sre-primary")
    return {"status": Status.ESCALATED.value,
            "timeline": say(f"📟 Escalated to on-call via PagerDuty (reason: {reason}); full context attached")}


@agent_node("close")
async def close(state: IncidentState) -> dict[str, Any]:
    inc = state["incident"]
    mttr = round(time.time() - inc["opened_at"], 2)
    METRICS.observe("incident.mttr_s", mttr)
    AUDIT.record(inc["id"], "supervisor", "incident.closed", mttr_s=mttr, tokens_used=state.get("tokens_used"),
                 risk=state.get("risk"), approver=(state.get("approval") or {}).get("approver"))
    return {"status": Status.CLOSED.value,
            "timeline": say(f"🏁 Incident {inc['id']} closed. MTTR {mttr}s · tokens {state.get('tokens_used')}")}


__all__ = ["intake", "supervisor", "triage", "planner", "risk_gate", "hitl_request", "hitl_gate", "executor",
           "verifier", "escalate", "close", "route_from_supervisor", "route_from_risk_gate", "route_from_hitl",
           "route_from_executor", "set_emitter", "RiskClass"]
