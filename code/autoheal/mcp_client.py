"""MCP client gateway used by agents: identity injection, retries, circuit breaker, OTel propagation.

Transport: in-process (FastMCP in-memory) by default, or Streamable HTTP when AUTOHEAL_MCP_URL is set.
"""
from __future__ import annotations

from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError
from opentelemetry import propagate

from .config import SETTINGS
from .resilience import CircuitBreaker, TransientError, retry_async
from .telemetry import METRICS, span

_BREAKERS: dict[str, CircuitBreaker] = {}


class MCPGateway:
    def __init__(self, caller: str, incident_id: str, on_event=None):
        self.caller, self.incident_id = caller, incident_id
        self.on_event = on_event or (lambda msg: None)

    def _target(self):
        if SETTINGS.mcp_url:
            return SETTINGS.mcp_url
        from .mcp_server import mcp  # in-process transport
        return mcp

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        breaker = _BREAKERS.setdefault(tool, CircuitBreaker(f"mcp:{tool}", failure_threshold=3, reset_timeout_s=30))
        with span("mcp.tool_call", **{"mcp.tool": tool, "enduser.id": self.caller}) as s:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            payload = {"incident_id": self.incident_id, "caller": self.caller,
                       "traceparent": carrier.get("traceparent"), **args}

            async def once() -> dict[str, Any]:
                METRICS.inc("tool.calls")
                try:
                    async with Client(self._target()) as client:
                        res = await client.call_tool(tool, payload)
                        return res.data if res.data is not None else res.structured_content
                except ToolError as exc:
                    METRICS.inc("tool.failures")
                    if str(exc).startswith("TRANSIENT"):
                        raise TransientError(str(exc)) from exc
                    raise

            def log_retry(attempt: int, exc: Exception) -> None:
                self.on_event(f"   ↻ retry {attempt} for {tool}: {exc}")
                s.add_event("retry", {"attempt": attempt})

            result = await breaker.call(lambda: retry_async(once, attempts=3, on_retry=log_retry))
            s.set_attribute("outcome", "ok")
            return result
