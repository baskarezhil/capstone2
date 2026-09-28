"""Simulated multi-cloud control plane (AWS EKS + Azure AKS) so the demo runs without a cluster.

Swap for real adapters (kubernetes / boto3 / azure-mgmt clients) behind the MCP server;
nothing above the MCP boundary changes.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from .config import ROOT

SCENARIO_DIR = ROOT / "scenarios"
_MEM = {"Mi": 1, "Gi": 1024}


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


def load_scenario(name: str) -> dict[str, Any]:
    return json.loads((SCENARIO_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _mebibytes(q: str) -> int:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", q)
    return int(m.group(1)) * _MEM[m.group(2)] if m else 0


class SimulatedCloud:
    def __init__(self) -> None:
        self.services: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.transient_failures_left = 0

    def seed(self, scenario: dict[str, Any]) -> None:
        lab = scenario["alert"]["labels"]
        self.services[(lab["cluster"], lab["namespace"], lab["service"])] = deepcopy(scenario["service_state"]) | {
            "logs": list(scenario["logs"])}
        self.transient_failures_left = scenario.get("transient_failures", 0)

    def _svc(self, cluster: str, namespace: str, deployment: str) -> dict[str, Any]:
        try:
            return self.services[(cluster, namespace, deployment)]
        except KeyError:
            raise LookupError(f"deployment {cluster}/{namespace}/{deployment} not found") from None

    def maybe_fail(self) -> None:
        if self.transient_failures_left > 0:
            self.transient_failures_left -= 1
            raise TimeoutError("kube-apiserver request timed out (simulated)")

    def logs(self, cluster: str, namespace: str, deployment: str, tail: int) -> str:
        return "\n".join(self._svc(cluster, namespace, deployment)["logs"][-tail:])

    def health(self, cluster: str, namespace: str, deployment: str) -> dict[str, Any]:
        s = self._svc(cluster, namespace, deployment)
        return {k: s[k] for k in ("status", "error_rate", "replicas", "ready", "restarts", "memory_limit", "revision")}

    def restart(self, cluster: str, namespace: str, deployment: str) -> dict[str, Any]:
        s = self._svc(cluster, namespace, deployment)
        s["revision"] += 1
        if s["issue"] == "stale_cache":
            s.update(status="healthy", error_rate=0.002, ready=s["replicas"], issue=None)
            s["logs"].append("INFO  catalog cache rebuilt from schema v42; checksum OK")
        return {"rolled_out": True, "revision": s["revision"]}

    def dry_run_patch(self, cluster: str, namespace: str, deployment: str, patch: dict[str, Any]) -> tuple[bool, str]:
        self._svc(cluster, namespace, deployment)
        for c in patch.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            lim = c.get("resources", {}).get("limits", {}).get("memory")
            if lim and not 256 <= _mebibytes(lim) <= 4096:
                return False, f"admission webhook: memory limit {lim} outside allowed range 256Mi-4Gi"
        return True, "server dry-run OK"

    def apply_patch(self, cluster: str, namespace: str, deployment: str, patch: dict[str, Any]) -> dict[str, Any]:
        s = self._svc(cluster, namespace, deployment)
        s["revision"] += 1
        for c in patch.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            lim = c.get("resources", {}).get("limits", {}).get("memory")
            if lim:
                s["memory_limit"] = lim
        if s["issue"] == "memory_exhaustion" and _mebibytes(s["memory_limit"]) >= 1024:
            s.update(status="healthy", error_rate=0.001, ready=s["replicas"], issue=None)
            s["logs"].append(f"INFO  rollout complete: revision {s['revision']}, 0 OOMKilled in 5m")
        return {"applied": True, "revision": s["revision"], "memory_limit": s["memory_limit"]}


CLOUD = SimulatedCloud()
