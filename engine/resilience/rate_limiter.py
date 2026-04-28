"""Per-domain async token bucket. Prevents request flooding.

Each domain gets a bucket sized at `rps * 2` (two seconds of burst).
`acquire()` is async; it sleeps until a token is available.

Time and sleep are injectable so tests run without real waits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from engine.config.defaults import DEFAULTS
from engine.config.domains import DOMAIN_CONFIG


@dataclass
class _Bucket:
    tokens: float
    max_tokens: float
    refill_per_ms: float
    last_refill_ms: int


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RateLimiter:
    def __init__(
        self,
        now_fn: Callable[[], int] = _now_ms,
        sleep_fn: Callable[[float], Awaitable[None]] = _async_sleep,
    ) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._now = now_fn
        self._sleep = sleep_fn

    def _bucket(self, domain: str) -> _Bucket:
        b = self._buckets.get(domain)
        if b is not None:
            return b
        rps = DOMAIN_CONFIG.get(domain, {}).get("rps", DEFAULTS.DEFAULT_RPS)
        max_tokens = max(rps * 2.0, 1.0)
        b = _Bucket(
            tokens=max_tokens,
            max_tokens=max_tokens,
            refill_per_ms=rps / 1000.0,
            last_refill_ms=self._now(),
        )
        self._buckets[domain] = b
        return b

    def _refill(self, b: _Bucket) -> None:
        now = self._now()
        delta_ms = max(0, now - b.last_refill_ms)
        b.tokens = min(b.max_tokens, b.tokens + delta_ms * b.refill_per_ms)
        b.last_refill_ms = now

    async def acquire(self, domain: str) -> None:
        b = self._bucket(domain)
        while True:
            self._refill(b)
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return
            need = 1.0 - b.tokens
            wait_ms = need / b.refill_per_ms if b.refill_per_ms > 0 else 5_000
            await self._sleep(min(wait_ms, 5_000) / 1000.0)

    def stats(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for domain, b in self._buckets.items():
            out[domain] = {
                "rps": b.refill_per_ms * 1000.0,
                "tokens": round(b.tokens, 2),
            }
        return out
