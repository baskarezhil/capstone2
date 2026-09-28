import asyncio

import pytest

from langgraph.checkpoint.memory import InMemorySaver

from autoheal import agents
from autoheal.graph import build_graph, resume_incident, start_incident
from autoheal.llm import ROUTER, MockLLM
from autoheal.telemetry import METRICS

agents.set_emitter(lambda msg: None)


@pytest.fixture(autouse=True)
def fresh_cache():
    ROUTER.cache.clear()     # each test sees its own RCA, not one cached by a previous test


def run(coro):
    return asyncio.run(coro)


def test_executor_only_reachable_through_hitl_gate():
    edges = build_graph().get_graph().edges
    into_executor = {e.source for e in edges if e.target == "executor"}
    assert into_executor == {"hitl_gate"}


def test_non_destructive_incident_runs_autonomously():
    app = build_graph(InMemorySaver())
    _, result = run(start_incident(app, "stale-cache"))
    assert "__interrupt__" not in result
    assert result["status"] == "closed" and result["risk"] == "NON_DESTRUCTIVE"
    assert result["approval"]["approver"] == "policy:auto-pass"


def test_destructive_incident_pauses_then_resumes_on_approval():
    app = build_graph(InMemorySaver())
    inc_id, result = run(start_incident(app, "oom-hotfix"))
    card = result["__interrupt__"][0].value
    assert card["risk"] == "DESTRUCTIVE" and card["proposed_action"]["tool"] == "apply_hotfix"
    assert card["rollback_plan"] and card["diff"]
    result = run(resume_incident(app, inc_id, {"decision": "approve", "approver": "sre.alice"}))
    assert result["status"] == "closed" and result["exec_result"]["applied"]
    assert result["approval"]["token"] == "[consumed]"


def test_rejection_escalates_without_touching_infra():
    app = build_graph(InMemorySaver())
    inc_id, _ = run(start_incident(app, "oom-hotfix"))
    result = run(resume_incident(app, inc_id, {"decision": "reject", "approver": "sre.alice", "comment": "freeze"}))
    assert result["status"] == "escalated" and result.get("exec_result") is None


def test_modify_requires_fresh_approval_for_new_patch():
    app = build_graph(InMemorySaver())
    inc_id, first = run(start_incident(app, "oom-hotfix"))
    second = run(resume_incident(app, inc_id, {"decision": "modify", "approver": "sre.bob",
                                               "changes": {"memory_limit": "2Gi"}}))
    card1, card2 = first["__interrupt__"][0].value, second["__interrupt__"][0].value
    assert card1["proposed_action"]["patch_id"] != card2["proposed_action"]["patch_id"]
    done = run(resume_incident(app, inc_id, {"decision": "approve", "approver": "sre.bob"}))
    assert done["exec_result"]["memory_limit"] == "2Gi"


def test_prompt_injection_is_quarantined_and_malicious_plan_blocked():
    app = build_graph(InMemorySaver())
    blocks_before = METRICS.counters["guardrail.blocks"]
    inc_id, result = run(start_incident(app, "prompt-injection"))
    assert len(result["evidence"]["injections"]) == 2
    assert METRICS.counters["guardrail.blocks"] == blocks_before + 1
    assert "remote script pipe-to-shell" in result["guardrail_feedback"]
    card = result["__interrupt__"][0].value            # safe re-plan still needs a human
    assert "curl" not in card["proposed_action"]["command_preview"]


def test_non_destructive_action_needs_human_when_confidence_below_085(monkeypatch):
    real_rca = MockLLM._rca
    monkeypatch.setattr(MockLLM, "_rca", lambda self, p: {**real_rca(self, p), "confidence": 0.8})
    app = build_graph(InMemorySaver())
    inc_id, result = run(start_incident(app, "stale-cache"))
    card = result["__interrupt__"][0].value            # restart_service is NON_DESTRUCTIVE, but C=0.8 < 0.85
    assert card["risk"] == "NON_DESTRUCTIVE" and "0.85" in card["hitl_reason"]
    done = run(resume_incident(app, inc_id, {"decision": "approve", "approver": "sre.alice"}))
    assert done["status"] == "closed" and done["approval"]["approver"] == "sre.alice"


def test_low_confidence_fails_closed():
    app = build_graph(InMemorySaver())
    _, result = run(start_incident(app, "unknown-alert"))
    assert result["status"] == "escalated" and result.get("plan") is None
