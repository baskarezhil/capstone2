"""HITL decision gate support (ADR 01/02).

- build_approval_request(): the payload shown to the SRE (Slack/Teams card, A2UI-style).
- issue_token() / verify_token(): HMAC-signed, single-use, time-boxed approval tokens bound
  to (incident_id, patch_id). The MCP server refuses DESTRUCTIVE calls without a valid one,
  so a hijacked agent cannot approve its own action.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import SETTINGS
from .guardrails import OutputGuardrail
from .models import patch_digest

_scrub = OutputGuardrail().scrub

_USED_NONCES: set[str] = set()  # production: Redis SETNX with TTL


def build_approval_request(state: dict[str, Any]) -> dict[str, Any]:
    """The card leaves the trust boundary (Slack/Teams), so it is scrubbed for secrets/PII."""
    inc, rca, plan = state["incident"], state["rca"], state["plan"]
    return _scrub({
        "type": "approval_request",
        "incident_id": inc["id"],
        "severity": inc["alert"].get("severity", "P1"),
        "title": f"[{inc['alert'].get('severity', 'P1')}] {inc['alert']['summary']}",
        "target": f"{inc['cluster']}/{inc['namespace']}/{inc['service']}",
        "risk": state["risk"],
        "hitl_reason": state.get("hitl_reason"),
        "root_cause": rca.get("root_cause"),
        "confidence": rca.get("confidence"),
        "proposed_action": {
            "tool": plan["tool"],
            "intent": plan.get("intent"),
            "command_preview": plan.get("command_preview"),
            "patch_id": plan["args"].get("patch_id") or patch_digest(plan["args"]),
        },
        "diff": plan.get("diff"),
        "blast_radius": plan.get("blast_radius"),
        "rollback_plan": plan.get("rollback"),
        "guardrail_findings": state.get("evidence", {}).get("injections", []),
        "options": ["approve", "reject", "modify"],
        "expires_in_s": SETTINGS.approval_ttl_seconds,
    })


def _sign(body: bytes) -> str:
    return hmac.new(SETTINGS.hitl_secret.encode(), body, hashlib.sha256).hexdigest()


def issue_token(incident_id: str, patch_id: str, approver: str) -> str:
    claims = {"inc": incident_id, "patch": patch_id, "sub": approver,
              "exp": int(time.time()) + SETTINGS.approval_ttl_seconds, "nonce": secrets.token_hex(8)}
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode())
    return f"{body.decode()}.{_sign(body)}"


def verify_token(token: str | None, incident_id: str, patch_id: str, consume: bool = True) -> tuple[bool, str]:
    if not token or "." not in token:
        return False, "missing approval token"
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body.encode())):
        return False, "bad signature"
    claims = json.loads(base64.urlsafe_b64decode(body))
    if claims["exp"] < time.time():
        return False, "approval expired"
    if claims["inc"] != incident_id or claims["patch"] != patch_id:
        return False, "token not bound to this incident/patch (tampered plan?)"
    if claims["nonce"] in _USED_NONCES:
        return False, "token already used (replay)"
    if consume:
        _USED_NONCES.add(claims["nonce"])
    return True, claims["sub"]
