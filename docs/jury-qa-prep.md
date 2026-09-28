# Jury Q&A Prep (3 minutes)

Likely questions, grouped by rubric area. Each has a short answer and the evidence to point to. The owner is the role that should answer.

## Core integration (Lead Architect, Code Engineers)

**Why hierarchical and not P2P or swarm?**
Only a hierarchy guarantees the HITL gate is on the *only* path to execution. The graph has exactly one edge into the executor, from the HITL gate, and a test asserts it. *Evidence:* ADR 01, `test_executor_only_reachable_through_hitl_gate`.

**What if the orchestrator pod dies while waiting for approval?**
State is checkpointed per incident. We showed a pause in one process and an approval from a different process that resumed from the checkpoint. *Evidence:* demo `--decision pause` then `approve INC-…`; SQLite in the prototype, Redis/Postgres in production.

**What happens when a tool keeps failing?**
The call is retried with backoff up to 3 times. A per-tool circuit breaker opens after 3 failures. After 2 failed remediation attempts the supervisor escalates to on-call. *Evidence:* `resilience.py`, stale-cache demo retry.

**Why MCP instead of calling the cloud SDKs directly?**
It gives one policy enforcement point, credentials never reach agents, the tools are typed and narrow, and it is language-neutral. *Evidence:* ADR 02; the same flow runs over Streamable HTTP.

## Telemetry & audit (Security & Observability Lead)

**Can you prove who approved a change?**
Yes. The approval record holds the approver, decision, timestamp and patch digest in a hash-chained log. Editing any record breaks verification. *Evidence:* `python -m autoheal audit --incident …`, `test_audit_chain_detects_tampering`.

**Is the trace end-to-end, including the tool server?**
Yes. We inject a W3C traceparent into every MCP call, so the server's spans nest under the agent's span. *Evidence:* trace tree on slide 7 / `--trace`.

**Your cache hit ratio and tool failure rate miss target. Why?**
The demo has only 6 incidents, and only repeated incidents can hit the cache; the projected production figure is 41%. The failures were injected on purpose to test retry, and none went unrecovered. *Evidence:* NFR matrix notes.

## Contract compliance (Security & Governance Lead)

**Could a prompt injection make the agent delete a namespace?**
It would have to beat L2 quarantine, then the L3 deny-list in the graph *and again* in the MCP server, then OPA policy, and then produce an SRE-signed token bound to that exact patch. The prompt-injection demo shows L2 and L3 catching it. *Evidence:* ADR 04, prompt-injection scenario.

**What stops the plan from being changed after the SRE approves it?**
The token is bound to the SHA-256 digest of the patch. The MCP server recomputes the digest and rejects any mismatch. *Evidence:* `test_hotfix_with_tampered_patch_is_denied`.

**What about data exfiltration?**
Outbound transfer tools and reads of secret material are on the deny-list. Logs are redacted before they reach any LLM. Model output and the Slack card are scrubbed of secrets. *Evidence:* ADR 04 (L1, L3, L4), `test_output_guardrail_scrubs_approval_card…`.

**How does the brief's confidence rule (C < 0.85) work?**
Even a non-destructive fix needs an SRE when C < 0.85. Below 0.5, nothing is planned and on-call is paged. *Evidence:* `test_non_destructive_action_needs_human_when_confidence_below_085`.

**Where is the DDD alignment?**
Incident Remediation is the Core domain. Approval, Infrastructure Control and Guardrails are Supporting domains. Ingestion, LLM Gateway and Observability are Generic. MCP is the Open Host Service. *Evidence:* `docs/ddd-context-map.md`.

## Money & NFRs (FinOps & NFR Lead)

**Your ROI depends on your assumptions. What if they're wrong?**
Agreed, and the workbook shows it: the conservative case gives −23% ROI. The one number that decides the case is the cost of a P1 hour; break-even is $1,021. Everything recalculates if you change it on the Assumptions sheet.

**Token cost is tiny. Why bother with routing and caching?**
Tokens are 0.4% of TCO, so the main reasons are latency and resilience during alert storms. The 83% LLM saving is a bonus. We say so in ADR 03.

**Is the latency figure real?**
Yes. It was measured on real gpt-4o-mini and gpt-4o calls: p95 per agent step is 4.39 s, which **misses** the 3.5 s target. The slow steps are the Tier-2 planner (about 4.0 s average) and the verifier's model call (about 3.4 s). That verifier call is no longer authoritative, because health is checked in code, so the pilot moves it off the critical path and streams Tier-2 output.

**Did anything break when you switched to a real model?**
Two things, both fixed and covered by tests. The model returned `confidence` as a string, which is now converted and fails closed if it isn't a number. gpt-4o-mini also misjudged "0.002 > 0.01" and kept restarting a healthy service, so SLO checks are now deterministic and the model only explains. Our lesson: never let an LLM do arithmetic on a safety decision.

## If you don't know
"We haven't measured that yet. It's on the pilot plan, and here's how we'd measure it: …" Never invent a number.
