# Pod 2: AIOps Incident Remediation & Auto-Healing (prototype)

A runnable multi-agent orchestrator for **CloudScale Global Networks**, with:
- a **LangGraph** orchestrator using a hierarchical supervisor;
- a **FastMCP** control-plane server;
- a **HITL pause/resume gate** backed by a durable checkpoint;
- **guardrail middleware**, a **hash-chained audit log** and **OpenTelemetry tracing**.

It runs fully offline: the clusters and the LLM are simulated deterministically, so the demo needs no API keys and no Kubernetes.

Design: [architecture](../diagrams/01-cloud-architecture.excalidraw) · [workflow](../diagrams/02-agent-workflow-sequence.excalidraw) · [ADR 01](../docs/adr/ADR-01-agent-topology-and-orchestration.md) · [ADR 02](../docs/adr/ADR-02-tooling-protocol-strategy.md) · [ADR 03](../docs/adr/ADR-03-cost-performance-strategy.md)

## Quick start

```powershell
cd code
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:PYTHONWARNINGS="ignore"            # hides third-party deprecation noise

.venv\Scripts\python -m autoheal demo   # all scenarios, non-interactive (≈2 s)
.venv\Scripts\python -m pytest          # 59 tests
```

On macOS/Linux, use `python3 -m venv .venv && .venv/bin/python ...`.

## Web console (UI)

```powershell
.venv\Scripts\python -m autoheal ui        # opens http://127.0.0.1:8080 (localhost only); Ctrl+C to stop
```

- **Scenarios:** click **Run**. The timeline fills in step by step as each agent finishes.
- **Approval card:** appears when a fix is destructive or confidence is below 0.85. It shows the root cause, command preview, colour-coded diff, blast radius and rollback. Choose **Approve**, **Modify & re-plan** (new memory limit) or **Reject**.
- **Right-hand panels:** live metrics (tokens, cost, CR, FR, p95 latency, guardrail counts) and the incident's audit trail with hash-chain status.
- **Reasoning toggle:** switches between MockLLM and OpenAI. OpenAI is enabled only if `OPENAI_API_KEY` is set in the terminal that started the UI.
- **Shared state with the CLI:** the console uses the same checkpointer and audit log, so an incident paused in the browser can be approved with `python -m autoheal approve INC-…`, and vice versa. Deep link to an incident with `/?incident=INC-…`.

## Run with real OpenAI models

```powershell
$env:OPENAI_API_KEY = "sk-..."            # set in this terminal only; never commit it
.venv\Scripts\python -m autoheal check-llm # one tiny call per tier: proves key, models, JSON mode
$env:AUTOHEAL_LLM = "litellm"              # Tier 1 = openai/gpt-4o-mini, Tier 2 = openai/gpt-4o
.venv\Scripts\python -m autoheal run --scenario oom-hotfix --trace
.venv\Scripts\python -m autoheal demo --metrics-out var/prototype_metrics_real.json
```

- Change models with `AUTOHEAL_TIER1_MODEL` / `AUTOHEAL_TIER2_MODEL` (any LiteLLM model string).
- Token counts and cost come from the provider's reported usage at the workbook's list prices.
- The clusters stay simulated; only the reasoning becomes real.
- What changes with a real model: its answers are not deterministic, so confidences, plans and cache hits vary from run to run. The deterministic controls do not change: the target is pinned to the incident (a model cannot redirect a fix to another cluster), missing fields fail closed (confidence 0 → human or escalation), "healthy" needs the real health signal to agree, and every guardrail, policy and approval check is identical.
- `prompt-injection` shows real L2 quarantine. The "compromised planner" step is a MockLLM simulation, so with a real model, L3 only fires if the model proposes something dangerous.
- A full `demo` run should cost a few cents at list prices (an estimate; the metrics summary prints the actual `llm_cost_usd`).
- **First real run (2026-09-28):** all 6 incidents correct, 11,205 tokens, $0.034, p95 step latency 4.39 s. It surfaced two bugs the mock hid, both now fixed and tested: confidence returned as a string (now coerced, fail-closed) and a wrong numeric comparison in the verifier (SLO check is now deterministic).
- Feed a real run into the workbook: `demo --metrics-out var/real/prototype_metrics_real.json`, then `python ../finance/build_workbook.py <out.xlsx> var/prototype_metrics.json var/real/prototype_metrics_real.json`.

## Scenarios (what to show the jury)

| Command | Shows |
|---|---|
| `python -m autoheal run --scenario stale-cache --trace` | **Autonomous path.** A NON_DESTRUCTIVE rolling restart is auto-approved. A transient kube-apiserver timeout is retried. The full OTel span tree is printed, with MCP server spans joined to the agent trace. |
| `python -m autoheal run --scenario oom-hotfix` | **HITL gate.** A DESTRUCTIVE config patch pauses the graph and prints the approval card (RCA, diff, blast radius, rollback). You answer approve, reject, modify or pause interactively. |
| `python -m autoheal run --scenario oom-hotfix --decision pause` then `python -m autoheal approve INC-… --approver sre.alice` | **Durable pause/resume.** The incident is saved to SQLite, the process exits, and a *different process* resumes it from the checkpoint after approval. |
| `python -m autoheal run --scenario oom-hotfix --decision modify --memory 2Gi` | The SRE edits the fix. The planner re-plans, the new patch gets a **new digest**, and it must be approved again. |
| `python -m autoheal run --scenario prompt-injection` | **Guardrails.** A leaked AWS key is redacted and two injection lines in the logs are quarantined. A (simulated) compromised planner proposes `curl … \| sh` plus a privileged container. The **command guardrail blocks it** and a safe re-plan still needs SRE approval. |
| `python -m autoheal run --scenario unknown-alert` | **Fails closed.** The Tier-1 model's confidence is too low, so the call escalates to Tier 2. RCA confidence stays below 0.5, so no action is taken and on-call is paged. |
| `python -m autoheal run --scenario stale-cache --repeat 3` | **Semantic cache.** Runs 2 and 3 reuse the classification and RCA, cutting tokens by about 65%. |
| `python -m autoheal demo --metrics-out var/prototype_metrics.json` | Exports the measured metrics that feed the NFR SLA Matrix in the finance workbook. |
| `python -m autoheal audit --incident INC-…` | Audit trail for one incident, plus **tamper verification** of the hash chain. |
| `python -m autoheal.mcp_server --scenario oom-hotfix`, then set `AUTOHEAL_MCP_URL=http://127.0.0.1:8765/mcp` | The same flow over a real **Streamable HTTP** MCP transport instead of in-process. |

## Code map

```
autoheal/
  graph.py        LangGraph StateGraph wiring, SQLite checkpointer, start/resume helpers
  agents.py       Supervisor, Triage, Planner, Risk gate, HITL request/gate, Executor, Verifier, Escalate, Close
  mcp_server.py   FastMCP server: fetch_k8s_logs · get_service_health · restart_service · apply_hotfix
                  └ authZ scopes → validation → command guardrail → policy (OPA stand-in) → dry-run → creds → audit
  mcp_client.py   Agent-side gateway: identity, retry + backoff, circuit breaker, W3C traceparent injection
  guardrails.py   InputGuardrail (redaction + injection quarantine + spotlighting), CommandGuardrail (deny-list)
  risk.py         Deterministic risk classifier: READ / NON_DESTRUCTIVE / DESTRUCTIVE (unknown → DESTRUCTIVE)
  hitl.py         Approval card payload; HMAC single-use approval tokens bound to (incident, patch digest)
  llm.py          Model router (Tier 1/Tier 2 + confidence escalation), semantic cache, MockLLM, LiteLLM backend
  audit.py        Append-only, SHA-256 hash-chained audit log + verify_chain()
  telemetry.py    OTel tracer (in-memory + optional OTLP), capstone metrics, trace tree renderer
  resilience.py   CircuitBreaker, retry_async
  infra_sim.py    Simulated EKS/AKS clusters seeded from scenarios/*.json
scenarios/        stale-cache · oom-hotfix · prompt-injection · unknown-alert
tests/            guardrails, risk, cache, tokens, audit tamper, MCP zero-trust, graph paths (59 tests)
```

## How the brief's requirements map to code

| Requirement | Where |
|---|---|
| Triage & Diagnosis, Remediation Planner and Execution & Verification agents | `agents.py` (`triage`, `planner`, `executor`, `verifier`) |
| MCP tools `fetch_k8s_logs`, `restart_service`, `apply_hotfix` | `mcp_server.py` |
| Read-only and non-destructive actions run autonomously | `risk.py` + `hitl_gate` auto-pass (only when RCA confidence C ≥ 0.85) |
| HITL when confidence C < 0.85 (brief §2) | `risk_gate` sets `needs_human`; below 0.5 no plan is made and on-call is paged |
| Destructive, failover or config patch actions **must pause** for the SRE | `hitl_request` → `interrupt()` in `hitl_gate`. `executor` is only reachable from `hitl_gate` (see `test_executor_only_reachable_through_hitl_gate`) |
| Zero-trust command execution, block `rm -rf` and privilege escalation | `guardrails.CommandGuardrail`, applied in the risk gate **and** again inside the MCP server |
| State, retries, circuit breaker | SQLite checkpointer, `resilience.py`, supervisor attempt and token limits |
| Tracing, audit, metrics | `telemetry.py`, `audit.py`, metrics summary printed after every run |

## Demo simplifications (what production would change)

| Prototype | Production |
|---|---|
| `MockLLM`: deterministic reasoning | `AUTOHEAL_LLM=litellm` with `AUTOHEAL_TIER1_MODEL` / `AUTOHEAL_TIER2_MODEL` (prompts are already in `agents.py`) |
| Caller identity passed as a tool argument | OAuth 2.1 client credentials + mTLS; scopes taken from the token (FastMCP auth provider) |
| `policy_decision()` Python rules | OPA / Rego sidecar |
| Regex injection detector | Plus LlamaGuard 3 / Guardrails AI via `InputGuardrail(ml_classifier=...)` |
| Hashed bag-of-words embedding, in-process cache | Real embedding model + Redis vector index |
| SQLite checkpointer | Redis or Postgres checkpointer |
| JSONL audit file | Same records shipped to S3 Object Lock (WORM) and the SIEM |
| In-memory used-token set | Redis `SETNX` with TTL |
| `SimulatedCloud` | Kubernetes / AWS / Azure SDK adapters behind the same MCP tools |
