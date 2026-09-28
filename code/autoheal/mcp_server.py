"""Cloud Infrastructure Control Plane, exposed as an MCP server (FastMCP). See ADR 02.

Every tool call passes, in order:
  1. AuthZ         caller identity → tool scopes (least privilege per agent)
  2. Validation    typed params, k8s name regex, bounded ranges, cluster allow-list
  3. Guardrail     deterministic command/patch deny-list (L3)
  4. Policy        OPA-style rules: risk class × namespace × approval token
  5. Dry-run       server-side dry-run before any write
  6. Credentials   short-lived, per-call (Vault stand-in)
  7. Audit + OTel  hash-chained audit record and a span joined to the caller's trace

Run standalone (Streamable HTTP):  python -m autoheal.mcp_server --scenario oom-hotfix
"""
from __future__ import annotations

import argparse
import json
import secrets
from contextlib import contextmanager
from typing import Any, Iterator

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from opentelemetry import propagate

from .audit import AUDIT
from .config import SETTINGS
from .guardrails import CommandGuardrail, InputGuardrail
from .hitl import verify_token
from .infra_sim import CLOUD, load_scenario
from .models import RiskClass, patch_digest
from .risk import TOOL_RISK
from .telemetry import METRICS, tracer

mcp = FastMCP(
    "cloudscale-control-plane",
    instructions="Remediation-level operations on CloudScale EKS/AKS clusters. "
                 "Destructive tools require a signed SRE approval token.",
)

# Production: OAuth 2.1 client-credentials; scopes come from the token, not from a parameter.
AGENT_SCOPES: dict[str, set[str]] = {
    "triage-agent": {"fetch_k8s_logs", "get_service_health"},
    "planner-agent": set(),                                   # plans only, never touches infra
    "executor-agent": {"fetch_k8s_logs", "get_service_health", "restart_service", "apply_hotfix"},
}

_cmd_guard, _redactor = CommandGuardrail(), InputGuardrail()


def policy_decision(caller: str, tool: str, cluster: str, namespace: str) -> tuple[bool, str]:
    """Stand-in for OPA/Rego. Returns (allow, reason)."""
    risk = TOOL_RISK[tool]
    if tool not in AGENT_SCOPES.get(caller, set()):
        return False, f"{caller} is not authorized for {tool}"
    if cluster not in SETTINGS.allowed_clusters:
        return False, f"cluster {cluster} not in allow-list"
    if risk is not RiskClass.READ and namespace in SETTINGS.protected_namespaces:
        return False, f"writes to protected namespace {namespace} are denied"
    return True, "allow"


def _short_lived_creds(cluster: str) -> dict[str, Any]:
    return {"cluster": cluster, "lease_id": secrets.token_hex(6), "ttl_s": 300}  # Vault dynamic secret


@contextmanager
def _governed(tool: str, incident_id: str, caller: str, cluster: str, namespace: str, deployment: str,
              traceparent: str | None) -> Iterator[dict[str, Any]]:
    ctx = propagate.extract({"traceparent": traceparent} if traceparent else {})
    with tracer.start_as_current_span(f"mcp.server.{tool}", context=ctx) as s:
        s.set_attribute("mcp.tool", tool)
        s.set_attribute("enduser.id", caller)
        record: dict[str, Any] = {"tool": tool, "caller": caller,
                                  "target": f"{cluster}/{namespace}/{deployment}", "risk": TOOL_RISK[tool].value}
        for name, val in (("cluster", cluster), ("namespace", namespace), ("deployment", deployment)):
            if not _cmd_guard.valid_name(val):
                _deny(incident_id, record, f"invalid {name} identifier")
        allow, reason = policy_decision(caller, tool, cluster, namespace)
        if not allow:
            _deny(incident_id, record, reason)
        try:
            CLOUD.maybe_fail()
            yield record
        except TimeoutError as exc:
            AUDIT.record(incident_id, "mcp-server", "tool.error", **record, error=str(exc))
            raise ToolError(f"TRANSIENT: {exc}") from exc
        except LookupError as exc:
            AUDIT.record(incident_id, "mcp-server", "tool.error", **record, error=str(exc))
            raise ToolError(str(exc)) from exc
        s.set_attribute("outcome", record.get("outcome", "ok"))
        AUDIT.record(incident_id, "mcp-server", "tool.invoked", **record)


def _deny(incident_id: str, record: dict[str, Any], reason: str) -> None:
    METRICS.inc("guardrail.blocks")
    AUDIT.record(incident_id, "mcp-server", "tool.denied", **record, reason=reason)
    raise ToolError(f"DENIED: {reason}")


@mcp.tool
def fetch_k8s_logs(incident_id: str, caller: str, cluster: str, namespace: str, deployment: str,
                   since_minutes: int = 30, tail: int = 200, traceparent: str | None = None) -> dict[str, Any]:
    """READ. Fetch recent container logs for a deployment. Secrets are redacted server-side."""
    with _governed("fetch_k8s_logs", incident_id, caller, cluster, namespace, deployment, traceparent) as rec:
        if not (1 <= since_minutes <= 120 and 1 <= tail <= 2000):
            _deny(incident_id, rec, "since_minutes must be 1-120 and tail 1-2000")
        raw = CLOUD.logs(cluster, namespace, deployment, tail)
        text, redactions = _redactor.redact(raw)
        rec.update(lines=text.count("\n") + 1, redactions=redactions)
        return {"logs": text, "line_count": rec["lines"], "redactions": redactions}


@mcp.tool
def get_service_health(incident_id: str, caller: str, cluster: str, namespace: str, deployment: str,
                       traceparent: str | None = None) -> dict[str, Any]:
    """READ. Current rollout health: status, error rate, ready replicas, restarts."""
    with _governed("get_service_health", incident_id, caller, cluster, namespace, deployment, traceparent):
        return CLOUD.health(cluster, namespace, deployment)


@mcp.tool
def restart_service(incident_id: str, caller: str, cluster: str, namespace: str, deployment: str,
                    traceparent: str | None = None) -> dict[str, Any]:
    """NON_DESTRUCTIVE. Zero-downtime rolling restart (clears in-memory/pod caches)."""
    with _governed("restart_service", incident_id, caller, cluster, namespace, deployment, traceparent) as rec:
        creds = _short_lived_creds(cluster)
        result = CLOUD.restart(cluster, namespace, deployment)
        rec.update(result=result, creds_lease=creds["lease_id"], outcome="restarted")
        return result


@mcp.tool
def apply_hotfix(incident_id: str, caller: str, cluster: str, namespace: str, deployment: str,
                 patch: dict[str, Any], patch_id: str, approval_token: str,
                 traceparent: str | None = None) -> dict[str, Any]:
    """DESTRUCTIVE. Apply a Planner-generated config patch. Requires a signed SRE approval token
    bound to this incident and this exact patch (patch_id = content digest)."""
    with _governed("apply_hotfix", incident_id, caller, cluster, namespace, deployment, traceparent) as rec:
        rec["patch_id"] = patch_id
        if patch_digest(patch) != patch_id:
            _deny(incident_id, rec, "patch content does not match approved patch_id")
        target = patch.get("target", {})
        if (target.get("cluster"), target.get("namespace"), target.get("deployment")) != (cluster, namespace, deployment):
            _deny(incident_id, rec, "patch target differs from call target")
        verdict = _cmd_guard.check(json.dumps(patch))
        if not verdict.allowed:
            _deny(incident_id, rec, f"guardrail: {', '.join(verdict.violations)}")
        ok, approver_or_reason = verify_token(approval_token, incident_id, patch_id)
        if not ok:
            _deny(incident_id, rec, f"approval: {approver_or_reason}")
        dry_ok, dry_msg = CLOUD.dry_run_patch(cluster, namespace, deployment, patch["patch"])
        if not dry_ok:
            _deny(incident_id, rec, dry_msg)
        creds = _short_lived_creds(cluster)
        result = CLOUD.apply_patch(cluster, namespace, deployment, patch["patch"])
        rec.update(approved_by=approver_or_reason, dry_run=dry_msg, result=result,
                   creds_lease=creds["lease_id"], outcome="patched")
        return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the control-plane MCP server over Streamable HTTP")
    ap.add_argument("--scenario", default="oom-hotfix")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    CLOUD.seed(load_scenario(args.scenario))
    mcp.run(transport="http", host="127.0.0.1", port=args.port)
