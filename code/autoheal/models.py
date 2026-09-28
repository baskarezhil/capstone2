"""Domain model for the Incident Remediation bounded context (Core domain)."""
from __future__ import annotations

import hashlib
import json
import operator
from enum import Enum
from typing import Annotated, Any, TypedDict


class RiskClass(str, Enum):
    READ = "READ"
    NON_DESTRUCTIVE = "NON_DESTRUCTIVE"
    DESTRUCTIVE = "DESTRUCTIVE"


class Status(str, Enum):
    NEW = "new"
    DIAGNOSED = "diagnosed"
    PLANNED = "planned"
    BLOCKED = "blocked"              # guardrail rejected the plan → re-plan
    APPROVED = "approved"
    EXECUTED = "executed"
    VERIFY_FAILED = "verify_failed"
    RESOLVED = "resolved"
    REJECTED = "rejected"            # SRE rejected → hand over to humans
    ESCALATE = "escalate"
    ESCALATED = "escalated"
    CLOSED = "closed"


class IncidentState(TypedDict, total=False):
    """LangGraph state. Plain dicts only so the checkpointer can serialize it."""
    incident: dict[str, Any]        # id, scenario, alert, cluster, namespace, service, opened_at
    evidence: dict[str, Any]        # sanitized logs, health, guardrail findings
    rca: dict[str, Any]
    plan: dict[str, Any] | None     # the proposed action
    risk: str | None
    needs_human: bool
    hitl_reason: str | None
    guardrail_feedback: list[str]
    approval: dict[str, Any] | None
    exec_result: dict[str, Any] | None
    verification: dict[str, Any] | None
    attempts: int
    status: str
    tokens_used: Annotated[int, operator.add]
    timeline: Annotated[list[str], operator.add]


def patch_digest(patch: dict[str, Any]) -> str:
    """Content address of a hotfix. The SRE approves exactly this digest (ADR 02)."""
    canonical = json.dumps(patch, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
