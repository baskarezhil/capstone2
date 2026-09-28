import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from autoheal import hitl
from autoheal.audit import AuditLog
from autoheal.infra_sim import CLOUD, load_scenario
from autoheal.mcp_server import mcp
from autoheal.models import patch_digest

TARGET = {"cluster": "azure-aks-prod-weu", "namespace": "checkout", "deployment": "checkout"}
PATCH = {"target": TARGET, "type": "strategic_merge",
         "patch": {"spec": {"template": {"spec": {"containers": [
             {"name": "checkout", "resources": {"limits": {"memory": "1Gi"}}}]}}}}}


# ------------------------------------------------------------------ approval tokens
def test_token_roundtrip_and_single_use():
    t = hitl.issue_token("INC-1", "sha256:abc", "sre.alice")
    assert hitl.verify_token(t, "INC-1", "sha256:abc") == (True, "sre.alice")
    ok, why = hitl.verify_token(t, "INC-1", "sha256:abc")
    assert not ok and "replay" in why


def test_token_bound_to_patch_and_incident():
    t = hitl.issue_token("INC-1", "sha256:abc", "sre.alice")
    assert not hitl.verify_token(t, "INC-1", "sha256:OTHER", consume=False)[0]
    assert not hitl.verify_token(t, "INC-2", "sha256:abc", consume=False)[0]


def test_token_forgery_and_expiry(monkeypatch):
    t = hitl.issue_token("INC-1", "sha256:abc", "sre.alice")
    assert not hitl.verify_token(t[:-1] + ("0" if t[-1] != "0" else "1"), "INC-1", "sha256:abc")[0]
    monkeypatch.setattr(hitl.time, "time", lambda: 9e12)
    assert "expired" in hitl.verify_token(t, "INC-1", "sha256:abc")[1]


# ------------------------------------------------------------------ audit chain
def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.record("INC-1", "test", f"event.{i}", n=i)
    assert log.verify_chain()[0]
    lines = log.path.read_text().splitlines()
    row = json.loads(lines[1]); row["details"]["n"] = 99
    lines[1] = json.dumps(row)
    log.path.write_text("\n".join(lines) + "\n")
    ok, idx, msg = log.verify_chain()
    assert not ok and idx == 1 and "modified" in msg


# ------------------------------------------------------------------ MCP zero-trust boundary
def call(tool, **args):
    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, {"incident_id": "INC-T", **args})).data
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def seeded_cloud():
    CLOUD.seed(load_scenario("oom-hotfix"))


def test_triage_identity_cannot_write():
    with pytest.raises(ToolError, match="not authorized"):
        call("restart_service", caller="triage-agent", **TARGET)


def test_reads_allowed_for_triage():
    out = call("fetch_k8s_logs", caller="triage-agent", **TARGET)
    assert "OOMKilled" in out["logs"] and "Sup3rS3cret" not in out["logs"]   # redacted server-side


def test_hotfix_without_approval_is_denied():
    with pytest.raises(ToolError, match="approval"):
        call("apply_hotfix", caller="executor-agent", **TARGET, patch=PATCH,
             patch_id=patch_digest(PATCH), approval_token="forged.token")


def test_hotfix_with_tampered_patch_is_denied():
    pid = patch_digest(PATCH)
    token = hitl.issue_token("INC-T", pid, "sre.alice")
    evil = json.loads(json.dumps(PATCH)); evil["patch"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "4Gi"
    with pytest.raises(ToolError, match="does not match"):
        call("apply_hotfix", caller="executor-agent", **TARGET, patch=evil, patch_id=pid, approval_token=token)


def test_hotfix_with_valid_approval_succeeds():
    pid = patch_digest(PATCH)
    token = hitl.issue_token("INC-T", pid, "sre.alice")
    out = call("apply_hotfix", caller="executor-agent", **TARGET, patch=PATCH, patch_id=pid, approval_token=token)
    assert out["applied"] and out["memory_limit"] == "1Gi"


def test_protected_namespace_write_denied():
    with pytest.raises(ToolError, match="protected namespace"):
        call("restart_service", caller="executor-agent", cluster="aws-eks-prod-use1",
             namespace="kube-system", deployment="coredns")


def test_unlisted_cluster_denied():
    with pytest.raises(ToolError, match="allow-list"):
        call("get_service_health", caller="triage-agent", cluster="gke-rogue", namespace="a", deployment="b")
