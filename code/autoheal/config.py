"""Central settings. Every value can be overridden with an AUTOHEAL_* environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.getenv(f"AUTOHEAL_{name}", default)


@dataclass(frozen=True)
class Settings:
    # LLM (ADR 03): "mock" runs fully offline and deterministically; "litellm" calls real providers.
    llm_backend: str = _env("LLM", "mock")
    tier1_model: str = _env("TIER1_MODEL", "openai/gpt-4o-mini")
    tier2_model: str = _env("TIER2_MODEL", "openai/gpt-4o")
    tier1_confidence_floor: float = 0.7
    # $ per 1M tokens, same list prices as the finance workbook (real-provider runs)
    tier1_price_in: float = 0.15
    tier1_price_out: float = 0.60
    tier2_price_in: float = 2.50
    tier2_price_out: float = 10.00
    # $ per 1K tokens (blended in/out) used for mock-run cost estimates only
    tier1_cost_per_1k: float = 0.0004
    tier2_cost_per_1k: float = 0.0060

    # Semantic cache (ADR 03)
    cache_similarity_threshold: float = 0.92
    cache_ttl_seconds: int = 900

    # Orchestration limits (ADR 01)
    recursion_limit: int = 40         # worst case ≈ 30 steps: 3 plan/verify loops + HITL + escalation
    max_remediation_attempts: int = 2
    token_budget_per_incident: int = 60_000

    # HITL thresholds (brief §2: escalate when confidence C < 0.85)
    hitl_confidence_threshold: float = 0.85   # below → human approval even for non-destructive actions
    rca_min_confidence: float = 0.5           # below → no plan at all, page on-call

    # HITL approval tokens (ADR 02)
    hitl_secret: str = _env("HITL_SECRET", "dev-only-secret-change-me")
    approval_ttl_seconds: int = 900

    # Zero-trust policy (ADR 02)
    allowed_clusters: tuple[str, ...] = ("aws-eks-prod-use1", "azure-aks-prod-weu")
    protected_namespaces: tuple[str, ...] = ("kube-system", "prod-payments")

    # Storage
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "var"))))
    mcp_url: str | None = os.getenv("AUTOHEAL_MCP_URL")  # None → in-process MCP transport

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit_log.jsonl"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"


SETTINGS = Settings()
SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
