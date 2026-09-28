# Pod 2 Engineering Package: IT Incident Remediation & Auto-Healing

**CloudScale Global Networks.** A multi-agent platform that diagnoses P1/P2 infrastructure incidents across AWS EKS and Azure AKS and fixes them on its own when that is safe. It stops for a Site Reliability Engineer (SRE) whenever an action is destructive or its confidence is below 0.85.

## Deliverables (brief §4)

| # | Artifact | Where |
|---|---|---|
| 1 | System architecture & workflow diagrams (Excalidraw) | [diagrams/01-cloud-architecture.excalidraw](diagrams/01-cloud-architecture.excalidraw) · [diagrams/02-agent-workflow-sequence.excalidraw](diagrams/02-agent-workflow-sequence.excalidraw) (PNG previews alongside) |
| 2 | Executable code repository (orchestrator + MCP server + HITL + guardrails) | [code/](code/), start with [code/README.md](code/README.md) |
| 3 | Architectural Decision Records | [ADR 01 Topology](docs/adr/ADR-01-agent-topology-and-orchestration.md) · [ADR 02 MCP boundary](docs/adr/ADR-02-tooling-protocol-strategy.md) · [ADR 03 Cost & performance](docs/adr/ADR-03-cost-performance-strategy.md) · [ADR 04 Guardrails & security](docs/adr/ADR-04-guardrails-and-security.md) (added beyond the required three) |
| 4 | Financial & NFR workbook (3-year TCO, token economics, SLA matrix) | [finance/Pod2_Financial_NFR_Workbook.xlsx](finance/Pod2_Financial_NFR_Workbook.xlsx) |
| + | DDD context map (rubric §3) | [docs/ddd-context-map.md](docs/ddd-context-map.md) |
| + | Jury Q&A prep | [docs/jury-qa-prep.md](docs/jury-qa-prep.md) |
| + | Defense deck (7 min + Q&A) | Claude artifact "Pod 2 Capstone Defense: AIOps Auto-Healing" (share it before the defense) |

## Rubric map (brief §2)

| Parameter | Evidence |
|---|---|
| **1. Core integration path** | Hierarchical LangGraph supervisor with three agents (ADR 01). FastMCP server with 4 typed tools (ADR 02). SQLite checkpointer for durable pause/resume. Retry and circuit breaker in `code/autoheal/resilience.py` |
| **2. Telemetry & audit** | One OpenTelemetry trace per incident, with W3C traceparent carried across MCP. Hash-chained audit log with tamper test. Metrics T_lat, Tokens/sec, CR and FR exported by `python -m autoheal demo --metrics-out` |
| **3. Contract compliance** | HITL gate: destructive actions, or confidence C < 0.85, need a signed SRE approval. Five guardrail layers (ADR 04). DDD context map: Incident Remediation is Core; Approval, Infrastructure Control and Guardrails are Supporting; Ingestion, LLM Gateway and Observability are Generic ([docs/ddd-context-map.md](docs/ddd-context-map.md)) |
| **4. Engineering package** | 59 passing tests, 4 demo scenarios, 2 diagrams, 4 ADRs, and a workbook whose formulas all evaluate |

## Run it (about 1 minute)

```powershell
cd code
py -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
$env:PYTHONWARNINGS="ignore"
.venv\Scripts\python -m autoheal demo      # all scenarios
.venv\Scripts\python -m pytest             # 59 tests
```

## Honest limits

- The clusters are simulated (SimulatedCloud). The reasoning runs either offline (MockLLM) or on real OpenAI models. On real gpt-4o-mini / gpt-4o, all 6 demo incidents behaved correctly for $0.034, but **p95 step latency is 4.39 s against the 3.5 s target (NOT MET)**. The pilot fix is to stream Tier 2 and take the verifier's model call off the critical path.
- The demo takes some shortcuts: caller identity is passed as an argument instead of OAuth, the policy engine is Python rules instead of OPA, and injection detection is regex instead of LlamaGuard. Each shortcut and its production replacement is listed in [code/README.md](code/README.md).
- The business case rests on Pod assumptions (volumes, cost of a P1 hour, MTTR reduction). The conservative scenario gives −23% ROI, and break-even is a P1 hour costing $1,021.
