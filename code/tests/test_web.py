"""Web console API: run → pause → approve through HTTP, exactly as the browser does."""
import time

from starlette.testclient import TestClient

from autoheal import web
from autoheal.llm import ROUTER


def wait(client, inc_id, until=lambda s: s["status"] != "running", timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/incidents/{inc_id}").json()
        if until(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out; last state {s}")


def test_console_page_and_config():
    with TestClient(web.app) as c:
        assert "Incident Console" in c.get("/").text
        cfg = c.get("/api/config").json()
        assert {s["name"] for s in cfg["scenarios"]} >= {"stale-cache", "oom-hotfix", "prompt-injection", "unknown-alert"}
        assert cfg["mode"] == "mock"


def test_run_pause_approve_via_api():
    ROUTER.cache.clear()
    with TestClient(web.app) as c:
        inc_id = c.post("/api/run", json={"scenario": "oom-hotfix"}).json()["incident_id"]
        s = wait(c, inc_id)
        assert s["status"] == "awaiting_approval"
        assert s["approval_request"]["risk"] == "DESTRUCTIVE" and s["approval_request"]["diff"]
        r = c.post(f"/api/incidents/{inc_id}/decision", json={"decision": "approve", "approver": "sre.ui-test"})
        assert r.status_code == 200
        s = wait(c, inc_id)
        assert s["status"] == "closed" and s["exec_result"]["applied"]
        audit = c.get(f"/api/incidents/{inc_id}/audit").json()
        assert audit["chain_valid"] and any(e["event"] == "hitl.approved" and e["actor"] == "sre.ui-test"
                                            for e in audit["entries"])
        assert c.get("/api/metrics").json()["hitl_requests"] >= 1


def test_bad_requests_are_rejected():
    with TestClient(web.app) as c:
        assert c.post("/api/run", json={"scenario": "nope"}).status_code == 400
        inc_id = c.post("/api/run", json={"scenario": "stale-cache"}).json()["incident_id"]
        wait(c, inc_id)
        r = c.post(f"/api/incidents/{inc_id}/decision", json={"decision": "approve"})
        assert r.status_code == 409                       # closed incidents cannot be "approved"
        assert c.post(f"/api/incidents/{inc_id}/decision", json={"decision": "yolo"}).status_code == 400
