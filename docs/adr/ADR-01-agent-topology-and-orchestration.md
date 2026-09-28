# ADR 01: Multi-Agent Topology & Orchestration Framework

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-09-28 |
| **Deciders** | Pod 2: Lead Solution Architect, Code & Protocol Engineers |
| **Related** | [01-cloud-architecture](../../diagrams/01-cloud-architecture.excalidraw) · [02-agent-workflow-sequence](../../diagrams/02-agent-workflow-sequence.excalidraw) · ADR 02 · ADR 03 |

## Context

CloudScale Global Networks needs to cut MTTR for P1/P2 incidents across AWS and Azure. The platform must:

- run a fixed pipeline for each incident: **diagnose → plan → (approve) → execute → verify**;
- **pause for hours** while an SRE reviews a destructive action, then resume exactly where it stopped, even if a pod restarts in the meantime;
- be **deterministic and auditable**, so a post-mortem can replay who decided what, in which order and why;
- contain failures: a hallucinating or looping agent must not keep acting on production infrastructure;
- give write access to infrastructure to **as few components as possible** (zero trust).

Topologies considered (Day 1):

| Option | Fit for this problem |
|---|---|
| **Swarm** | Emergent behaviour and no single control point. Cannot guarantee that the HITL gate is always crossed. ❌ |
| **Peer-to-peer (P2P)** | Flexible, but any agent can call any other, so the approval gate can be bypassed and the control flow is hard to audit. ❌ |
| **Hierarchical (supervisor + workers)** | One owner of routing, budgets and state. The HITL gate is a node on the **only** path to execution. ✅ |

Frameworks considered:

| Framework | Durable pause/resume | Explicit graph / conditional edges | Notes |
|---|---|---|---|
| **LangGraph (Python)** | ✅ `interrupt()` + checkpointer (Redis/Postgres) | ✅ `StateGraph` | Native OTel/LangSmith tracing; works cleanly with FastMCP |
| CrewAI | ⚠️ HITL is mostly synchronous `human_input` | ⚠️ role/task abstraction, less explicit flow | Fast to prototype, weaker governance |
| Semantic Kernel (.NET) | ⚠️ via Process Framework | ✅ | Good fit only if the team is .NET-first |
| Spring AI (Java) | ⚠️ custom | ⚠️ | Would need more custom state code |

## Decision

1. **Topology: hierarchical.** A **Supervisor** node owns routing, step limits and the token budget for each incident. It delegates to three workers:
   - **Triage & Diagnosis Agent** correlates telemetry and produces the RCA with a confidence score. **Read-only tools.**
   - **Remediation Planner Agent** drafts the Terraform, Ansible or Kubernetes patch plus a rollback plan. **No tools that change infrastructure.**
   - **Execution & Verification Agent** runs the approved plan, then verifies health. It is the **only agent with write-capable MCP tools**.
2. Workers **never call each other directly**. Every hand-off goes through the Supervisor as a graph edge, so every transition is traced and audited.
3. A deterministic **Action Risk Classifier** (rules, not an LLM) and a **HITL Gate node** sit on the only path between Planner and Executor:
   - `READ` and `NON_DESTRUCTIVE` actions (for example `fetch_k8s_logs`, clearing pod caches) → the gate passes them automatically.
   - `DESTRUCTIVE` actions (`apply_hotfix`, cluster failover, config patch) → `interrupt()`. State is checkpointed and the graph pauses until an SRE approves, rejects or modifies the action.
   - **Confidence gate (brief §2):** RCA confidence C < 0.85 also requires SRE approval, even for non-destructive actions. C < 0.5 produces no plan at all and pages on-call.
4. **Framework: LangGraph (Python)** with a **Redis checkpointer** (Postgres as fallback). Each incident has one `thread_id`, so paused incidents survive restarts.
5. **Hard guardrails in the graph:** `recursion_limit` = 40 steps (worst case ≈ 30), a maximum of 2 remediation retries, and a token budget per incident. When a limit is hit, the Supervisor stops and escalates to on-call.

## Consequences

**Positive**
- The HITL gate **cannot be bypassed**: execution is only reachable through the gate node. This maps directly to the brief's rule that destructive actions must pause for SRE approval.
- Durable pause/resume: an SRE can approve 3 hours later and the graph continues from the saved checkpoint.
- Clear DDD mapping. Incident Remediation is the **Core domain** (Triage, Planner and Executor). Telemetry, approval, audit and identity are **Supporting domains** reached through well-defined interfaces.
- Least privilege: only one agent can ever touch infrastructure in write mode.
- One trace per incident (`trace_id` = `incident_id`) covering every agent, LLM and tool span.

**Negative / risks**
- The Supervisor is a central point of failure and a bottleneck. *Mitigation:* stateless replicas on EKS, with all state kept in the checkpointer.
- Somewhat higher latency than P2P, because every hand-off passes through the Supervisor. This is acceptable because the SLA is MTTR, measured in minutes, not milliseconds.
- LangGraph ties the team to Python. *Mitigation:* tools live behind MCP (ADR 02), so they stay language-neutral.
- The rule-based classifier must be maintained as new action types appear. *Mitigation:* unknown actions default to `DESTRUCTIVE` (**fail closed**).
