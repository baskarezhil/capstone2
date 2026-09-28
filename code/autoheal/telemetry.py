"""OpenTelemetry tracing + the four capstone metrics.

Spans: incident (root) → agent node → llm.call / mcp.tool_call, sharing one trace_id per run.
Export: in-memory (printed by the CLI) and, if OTEL_EXPORTER_OTLP_ENDPOINT is set and the
OTLP exporter is installed, to an OTel Collector (Tempo / Jaeger / LangSmith).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPANS = InMemorySpanExporter()
_provider = TracerProvider(resource=Resource.create({"service.name": "autoheal-orchestrator",
                                                     "deployment.environment": "capstone-demo"}))
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    except ImportError:
        pass
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("autoheal")


class Metrics:
    """Minimal in-process registry. Production: OTel metrics → Prometheus → Grafana."""

    def __init__(self) -> None:
        self.counters: dict[str, float] = defaultdict(float)
        self.timings: dict[str, list[float]] = defaultdict(list)

    def inc(self, name: str, value: float = 1) -> None:
        self.counters[name] += value

    def observe(self, name: str, seconds: float) -> None:
        self.timings[name].append(seconds)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - t0)

    def summary(self) -> dict[str, Any]:
        c = self.counters
        hits, misses = c["cache.hits"], c["cache.misses"]
        calls, fails = c["tool.calls"], c["tool.failures"]
        steps = sorted(x for k, v in self.timings.items() if k.startswith("task.") and k != "task.hitl_wait" for x in v)
        p95 = steps[min(len(steps) - 1, int(0.95 * len(steps)))] if steps else None
        return {
            "agent_step_latency_p95_s": round(p95, 3) if p95 is not None else None,
            "latency_per_task_s": {k.removeprefix("task."): round(sum(v) / len(v), 3)
                                   for k, v in self.timings.items() if k.startswith("task.")},
            "tokens_total": int(c["llm.tokens"]),
            "tokens_tier1": int(c["llm.tokens.tier1"]),
            "tokens_tier2": int(c["llm.tokens.tier2"]),
            "llm_cost_usd": round(c["llm.cost_usd"], 5),
            "tier2_escalations": int(c["llm.tier2_escalations"]),
            "cache_hit_ratio": round(hits / (hits + misses), 2) if hits + misses else None,
            "tool_calls": int(calls),
            "tool_failure_rate": round(fails / calls, 2) if calls else None,
            "guardrail_blocks": int(c["guardrail.blocks"]),
            "injections_quarantined": int(c["guardrail.injections"]),
            "hitl_requests": int(c["hitl.requests"]),
        }


METRICS = Metrics()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else str(v))
        yield s


def render_trace_tree() -> str:
    """Pretty-print finished spans as an indented tree (for the demo CLI)."""
    spans = SPANS.get_finished_spans()
    children: dict[int | None, list] = defaultdict(list)
    for s in spans:
        children[s.parent.span_id if s.parent else None].append(s)
    lines: list[str] = []

    def walk(parent: int | None, depth: int) -> None:
        for s in sorted(children.get(parent, []), key=lambda x: x.start_time):
            ms = (s.end_time - s.start_time) / 1e6
            extra = {k: v for k, v in s.attributes.items() if k in
                     ("gen_ai.request.model", "cache.hit", "mcp.tool", "risk", "outcome", "gen_ai.usage.total_tokens")}
            lines.append(f"{'  ' * depth}- {s.name} ({ms:.1f} ms) {extra if extra else ''}".rstrip())
            walk(s.context.span_id, depth + 1)

    walk(None, 0)
    return "\n".join(lines)
