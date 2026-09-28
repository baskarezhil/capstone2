# ADR 03: Cost & Performance Strategy (Semantic Caching, Model Routing, Guardrail Placement)

| | |
|---|---|
| **Status** | Accepted (numbers are targets, validated in the Financial & NFR Workbook) |
| **Date** | 2026-09-28 |
| **Deciders** | Pod 2: FinOps & NFR Lead, Security & Governance Lead, Lead Solution Architect |
| **Related** | ADR 01 · ADR 02 · Financial & NFR Workbook |

## Context

- Incidents are **bursty**. One outage can fire hundreds of near-duplicate alerts ("alert storm"), and each would otherwise trigger a full LLM triage.
- Workload mix per incident: many **cheap, repetitive** calls (alert classification, log summarisation, risk labelling) and a few **hard** calls (cross-signal RCA, remediation planning).
- Latency matters for MTTR, but correctness matters more: a wrong patch on production costs far more than any number of tokens.
- Guardrails add latency and cost. Placing them in the wrong layer either leaves gaps or taxes every call.

## Decision

### 1. Tiered model routing (LiteLLM gateway)

| Tier | Example models | Used for | Target share of calls |
|---|---|---|---|
| **Tier 1: small/fast** | GPT-4o-mini · Claude Haiku 4.5 | Alert classification, dedupe, log summarisation, verification checks | ~70% |
| **Tier 2: large/reasoning** | GPT-4o · Claude Sonnet 5 | Multi-signal RCA, remediation plan + rollback plan | ~30% |

- **Escalation rule:** if Tier 1 returns confidence < 0.7 or fails schema validation, retry once on Tier 2.
- **Planner output is always Tier 2.** We never save money on the step that writes production changes.
- **Fallback:** if the primary provider fails (for example Azure OpenAI), switch to a secondary (for example Bedrock) through the gateway, with the same prompt contract. A circuit breaker opens after 5 consecutive errors for 60 s.

### 2. Semantic cache (Redis + embeddings)

| Setting | Value | Why |
|---|---|---|
| What is cached | Triage/classification and RCA for alert signatures that are **semantically near-identical** | Alert storms repeat the same signature |
| Similarity threshold | cosine ≥ **0.92** | Keep false hits rare; tune from traces |
| TTL | **15 min** (triage) · **24 h** (runbook retrieval) | Infrastructure state changes quickly |
| Cache key includes | `cluster`, `namespace`, `service`, `alert_rule`, deployment version | Prevents reusing an answer across environments |
| **Never cached** | Remediation plans, anything carrying an `approval_token`, execution results | A stale plan could be applied to changed infrastructure |
| Target hit ratio | **≥ 35%** of Tier 1 calls during incidents | Tracked as `cache_hit_ratio` in Grafana |

Additional token savings:
- Prompt caching of static system prompts and tool schemas.
- Log pre-filtering: deduplicate lines and keep only ERROR/WARN plus a ±50-line window before the LLM sees them.
- A token budget per incident, enforced by the Supervisor (ADR 01).

### 3. Guardrail placement (layered, cheapest first)

| Layer | Where | Mechanism | Latency budget |
|---|---|---|---|
| **L1: Ingress** | API Gateway / Event Normalizer | Schema validation, secret & PII redaction (regex + Presidio), size limits | < 10 ms |
| **L2: Input to LLM** | Before every LLM call on untrusted text (alerts, logs) | LlamaGuard 3 / Guardrails AI prompt-injection detection; logs wrapped as data | < 150 ms, **Tier 1 path only once per incident** |
| **L3: Output / action** | Action Risk Classifier + MCP server | Deterministic command denylist/allowlist, JSON-schema validation of plans, OPA policy | < 20 ms |
| **L4: Human** | HITL gate | SRE approval for `DESTRUCTIVE` actions | minutes (by design) |

- Deterministic checks (L1, L3) run on **every** call. They are cheap and cannot be prompt-injected.
- The ML-based check (L2) runs once per untrusted input, not once per agent hop, so its cost is not paid again at each step.
- **The final safety control is deterministic (L3 + L4), never an LLM.**

## Consequences

**Positive**
- **~83% lower LLM spend** than an "all Tier 2, no cache" baseline in the workbook's Base case ($1,261 → $218 per month, blended cache ratio 41%). See `finance/Pod2_Financial_NFR_Workbook.xlsx`, sheet *Token Economics*.
- Token spend is **under 1% of the 3-year TCO**; people and infrastructure dominate. Routing and caching are therefore justified mainly by latency and alert-storm resilience, with cost as a secondary gain.
- Alert storms get much cheaper and faster: duplicate alerts hit the cache instead of the LLM.
- Guardrail cost is bounded and predictable, and the controls that prevent damage do not depend on a model behaving correctly.
- Provider fallback improves availability towards the **99.9%** platform SLA.

**Negative / risks**
- **Stale or false cache hits** could mislead triage. *Mitigation:* strict key scoping, short TTL, no caching of plans, and a metric/alert on cache-hit incidents that were reopened.
- Routing adds complexity and needs evaluation data. *Mitigation:* a weekly offline evaluation of Tier 1 against Tier 2 on sampled incidents, with thresholds tuned from LangSmith traces.
- Multi-provider fallback means maintaining prompt parity across models.
- L2 guardrail models add some latency to the first triage step.

## Metrics tracked (OTel → Grafana)

`latency_per_task_p95` · `tokens_per_incident` · `cost_per_incident_usd` · `cache_hit_ratio` · `tier2_escalation_rate` · `tool_failure_rate` · `guardrail_block_count` · `hitl_wait_time_p50`
