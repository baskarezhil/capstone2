"""The real-provider path (AUTOHEAL_LLM=litellm), exercised with a backend that answers the way real
models do: raw k8s patches, wrong targets, missing fields, optimistic verdicts, provider token usage."""
import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from autoheal import agents
from autoheal.audit import AUDIT
from autoheal.graph import build_graph, resume_incident, start_incident
from autoheal.llm import ROUTER
from autoheal.telemetry import METRICS

agents.set_emitter(lambda msg: None)
USAGE = {"prompt": 1000, "completion": 100}


class RealisticBackend:
    def __init__(self, plan=None, verify=None, memory="1Gi"):
        self.plan, self.verify, self.memory = plan, verify, memory

    def complete(self, model, task, prompt, payload):
        assert "Return JSON" in prompt          # JSON mode needs the word JSON in the prompt
        if task == "classify":
            out = {"category": "memory_exhaustion", "severity": "P1", "confidence": 0.9}
        elif task == "summarize_logs":
            out = {"summary": "OOMKilled after heap growth", "signatures": ["OutOfMemoryError"], "confidence": 0.9}
        elif task == "rca":
            out = {"category": "memory_exhaustion", "confidence": 0.9,
                   "root_cause": "Heap exceeds the 512Mi limit; db password=hunter2 seen in logs"}   # no evidence key
        elif task == "plan":
            out = self.plan if self.plan is not None else {
                "tool": "apply_hotfix", "intent": "config_patch",
                "args": {"cluster": "gke-somewhere-else", "namespace": "default", "deployment": "checkout",
                         "patch": {"spec": {"template": {"spec": {"containers": [
                             {"name": "checkout", "resources": {"limits": {"memory": self.memory}}}]}}}}},
                "command_preview": "kubectl patch deployment checkout -n checkout -p '...'"}
        else:
            out = self.verify if self.verify is not None else {"healthy": True, "reason": "looks fine"}
        return {**out, "_usage": dict(USAGE)}


@pytest.fixture(autouse=True)
def clean_cache():
    ROUTER.cache.clear()


def run(coro):
    return asyncio.run(coro)


def test_real_model_plan_is_pinned_to_incident_target_and_executes(monkeypatch):
    monkeypatch.setattr(ROUTER, "backend", RealisticBackend())
    tokens_before = METRICS.counters["llm.tokens"]
    app = build_graph(InMemorySaver())
    inc_id, result = run(start_incident(app, "oom-hotfix"))
    card = result["__interrupt__"][0].value
    assert card["target"] == "azure-aks-prod-weu/checkout/checkout"          # not gke-somewhere-else
    assert "hunter2" not in str(card)                                       # L4 scrub on real output
    assert result["rca"]["evidence"] == []                                  # missing key filled safely
    assert any(e["event"] == "plan.retargeted" for e in AUDIT.entries(inc_id))
    # classify + summarize + rca + plan before the pause, counted from provider usage (not the estimate)
    assert METRICS.counters["llm.tokens"] - tokens_before == 4 * sum(USAGE.values())
    done = run(resume_incident(app, inc_id, {"decision": "approve", "approver": "sre.alice"}))
    assert done["status"] == "closed" and done["exec_result"]["memory_limit"] == "1Gi"


@pytest.mark.parametrize("raw,expected", [("0.9", 0.9), (90, 0.9), ("85%", 0.85), ("high", 0.0), (None, 0.0), (-0.2, 0.0)])
def test_confidence_type_drift_is_normalised_fail_closed(raw, expected):
    # found in the first real gpt-4o-mini run: confidence came back as a string
    from autoheal.llm import coerce_types
    assert coerce_types({"confidence": raw})["confidence"] == pytest.approx(expected)
    assert coerce_types({"confidence": 1, "healthy": "false"})["healthy"] is False


def test_empty_model_answer_fails_closed(monkeypatch):
    monkeypatch.setattr(ROUTER, "backend", RealisticBackend(plan={}))
    _, result = run(start_incident(build_graph(InMemorySaver()), "oom-hotfix"))
    assert result["status"] == "escalated" and result.get("exec_result") is None


def test_model_math_error_on_slo_does_not_cause_restart_loop(monkeypatch):
    # Seen with real gpt-4o-mini: "error_rate 0.002 is greater than 0.01" → unhealthy. SLO check wins.
    monkeypatch.setattr(ROUTER, "backend", RealisticBackend(
        plan={"tool": "restart_service", "intent": "cache_reset", "args": {}}, verify={"healthy": False}))
    inc_id, result = run(start_incident(build_graph(InMemorySaver()), "stale-cache"))
    assert result["status"] == "closed" and result["attempts"] == 0
    assert any(e["event"] == "verification.model_disagreed" for e in AUDIT.entries(inc_id))


def test_hallucinated_recovery_is_not_trusted(monkeypatch):
    # Patch too small to fix the OOM, model still claims "healthy" → real health signal wins.
    monkeypatch.setattr(ROUTER, "backend", RealisticBackend(memory="768Mi"))
    app = build_graph(InMemorySaver())
    inc_id, result = run(start_incident(app, "oom-hotfix"))
    while "__interrupt__" in result:
        result = run(resume_incident(app, inc_id, {"decision": "approve", "approver": "sre.alice"}))
    assert result["status"] == "escalated"
    assert result["verification"]["healthy"] is False
