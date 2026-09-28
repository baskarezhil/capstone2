"""Action Risk Classifier (ADR 01). Deterministic rules, never an LLM, and fail closed."""
from __future__ import annotations

from typing import Any

from .config import SETTINGS
from .models import RiskClass

TOOL_RISK: dict[str, RiskClass] = {
    "fetch_k8s_logs": RiskClass.READ,
    "get_service_health": RiskClass.READ,
    "restart_service": RiskClass.NON_DESTRUCTIVE,   # rolling restart, clears in-memory caches
    "apply_hotfix": RiskClass.DESTRUCTIVE,          # config patch / failover / any write
}

DESTRUCTIVE_INTENTS = ("failover", "config_patch", "scale_to_zero", "delete", "drain", "rollback")


def classify(action: dict[str, Any]) -> RiskClass:
    tool = action.get("tool")
    risk = TOOL_RISK.get(tool, RiskClass.DESTRUCTIVE)  # unknown tool → DESTRUCTIVE (fail closed)
    args = action.get("args", {})
    if risk is not RiskClass.READ and args.get("namespace") in SETTINGS.protected_namespaces:
        return RiskClass.DESTRUCTIVE
    if action.get("intent") in DESTRUCTIVE_INTENTS:
        return RiskClass.DESTRUCTIVE
    return risk


def requires_human(risk: RiskClass | str) -> bool:
    return RiskClass(risk) is RiskClass.DESTRUCTIVE
