"""Resilience layer: circuit breaker + retry with exponential backoff (Day 1 patterns)."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """Retryable failure (timeouts, 5xx, throttling)."""


class CircuitOpenError(Exception):
    """Fast-fail: the dependency is known to be unhealthy."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout_s: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        if time.monotonic() - self.opened_at >= self.reset_timeout_s:
            return "HALF_OPEN"
        return "OPEN"

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.state == "OPEN":
            raise CircuitOpenError(f"circuit '{self.name}' is OPEN")
        try:
            result = await fn()
        except Exception:
            self.failures += 1
            if self.failures >= self.failure_threshold or self.state == "HALF_OPEN":
                self.opened_at = time.monotonic()
            raise
        self.failures, self.opened_at = 0, None
        return result


async def retry_async(fn: Callable[[], Awaitable[T]], attempts: int = 3, base_delay_s: float = 0.1,
                      retry_on: tuple[type[Exception], ...] = (TransientError,),
                      on_retry: Callable[[int, Exception], None] | None = None) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            await asyncio.sleep(base_delay_s * 2 ** (attempt - 1) * (1 + random.random() / 2))
    raise AssertionError("unreachable")
