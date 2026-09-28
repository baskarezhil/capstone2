# DDD Context Map: Pod 2 AIOps Auto-Healing

Rubric §3 asks for *"clean alignment with Bounded Contexts, mapping agent capabilities explicitly to Core vs. Supporting Domain services"*. This page maps each bounded context to the code that implements it.

## Bounded contexts

| Bounded context | Domain type | Why this type | Capabilities (agents / services) | Code |
|---|---|---|---|---|
| **Incident Remediation** | **Core** | CloudScale's differentiator: turning signals into a safe, verified fix faster than a human. Built in-house, never bought | Supervisor, Triage & Diagnosis agent, Remediation Planner agent, Execution & Verification agent, risk classifier | `agents.py`, `graph.py`, `risk.py`, `models.py` |
| **Change Approval & Governance** | Supporting | Required by the business contract (HITL), specific to us, but not the differentiator | HITL gate, approval card, signed approval tokens, hash-chained audit log | `hitl.py`, `audit.py` |
| **Infrastructure Control** | Supporting | Wraps cloud APIs behind remediation-level operations; the only context that can change infrastructure | MCP server tools, policy check, dry-run, short-lived credentials | `mcp_server.py`, `infra_sim.py` (→ real EKS/AKS adapters) |
| **Security Guardrails** | Supporting | Cross-cutting protection tuned to this domain's threats (ADR 04) | L1 redaction, L2 injection quarantine, L3 command deny-list, L4 output scrub | `guardrails.py` |
| **Signal Ingestion** | Generic | Commodity monitoring (Prometheus, Datadog, CloudWatch); we consume, not build | Alert webhooks, event normalization | Scenario loader + `intake` node (prototype); API Gateway + Event Normalizer (production) |
| **LLM Gateway** | Generic | Buyable (LiteLLM, provider APIs); we configure routing and caching only | Tier-1/Tier-2 routing, semantic cache, provider fallback | `llm.py` |
| **Observability** | Generic | OpenTelemetry standard; off-the-shelf backends | Tracing, metrics | `telemetry.py` |

## Relationships (context map)

```
 Signal Ingestion ──[ACL: Event Normalizer]──▶  INCIDENT REMEDIATION (Core)
                                                   │         │          │
                     Published Language:           │         │          │ Conformist
                     approval card schema          ▼         │          ▼
                     Change Approval & Governance ◀┘         │     LLM Gateway
                     (signs tokens that Infra Control verifies)
                                                             │ Open Host Service +
                                                             │ Published Language (MCP tool schemas)
                                                             ▼
                                                  Infrastructure Control ──▶ EKS / AKS / Vault
 Security Guardrails: Shared Kernel (guardrails.py) used by Remediation AND Infrastructure Control
 Observability: every context emits OTel spans and audit events (Generic, Conformist)
```

| Upstream → Downstream | Pattern | What it means here |
|---|---|---|
| Signal Ingestion → Incident Remediation | **Anti-Corruption Layer** | Vendor alert formats are translated into our `Incident` model before the Core sees them, so the Core never depends on Datadog/Prometheus shapes |
| Infrastructure Control → Incident Remediation | **Open Host Service + Published Language** | The MCP tool schemas are the contract. Agents speak "restart_service / apply_hotfix", never kubectl or cloud SDKs (ADR 02) |
| Change Approval → Infrastructure Control | **Customer / Supplier** | Infrastructure Control only accepts destructive calls carrying a token issued by Change Approval, bound to incident + patch digest |
| Security Guardrails ↔ Remediation, Infrastructure Control | **Shared Kernel** | The same deny-list runs in the graph and inside the MCP server, deliberately shared so both enforce identical rules |
| LLM Gateway → Incident Remediation | **Conformist** | We adopt the provider/LiteLLM interface as-is; prompts return JSON matching our model |

## Ubiquitous language (Core)

| Term | Meaning |
|---|---|
| Incident | One actionable problem; one `thread_id`, one trace, one audit trail |
| RCA / confidence C | Root cause and the Triage agent's confidence; C < 0.85 means a human decides, C < 0.5 means no plan |
| Plan / action | One typed tool call with command preview, diff, blast radius and rollback |
| Risk class | READ, NON_DESTRUCTIVE or DESTRUCTIVE; unknown is DESTRUCTIVE |
| Approval | An SRE's decision (approve / reject / modify) that yields a token bound to the patch digest |
| Escalation | Handing the incident to on-call with full context, the platform's safe failure mode |

## Honest gaps

- The Signal Ingestion **ACL is thin in the prototype**: scenarios are already in our schema. Production needs the Event Normalizer shown in the architecture diagram.
- Contexts are **Python modules in one process**, not separate services. The MCP boundary is the only one that is already network-ready (Streamable HTTP, demonstrated).
