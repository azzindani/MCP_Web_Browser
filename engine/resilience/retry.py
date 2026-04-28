"""Error classifier + exponential backoff with jitter.

`with_retry` wraps an async coroutine. The classifier inspects the
exception message; only `transient` and `rate_limit` errors are retried.
Backoff is `BACKOFF_BASE_MS * 2^(attempt-1) + jitter`, capped at
`BACKOFF_MAX_MS`. Rate-limit errors get a 3x longer base.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from engine.config.defaults import DEFAULTS

ErrorClass = Literal["transient", "rate_limit", "blocked", "not_found", "permanent"]
T = TypeVar("T")

OnRetry = Callable[[int, BaseException, float], None]


def classify_error(error: BaseException | str) -> ErrorClass:
    msg = (error if isinstance(error, str) else str(error)).lower()
    if "turnstile" in msg or "cf_clearance" in msg:
        return "blocked"
    if "404" in msg or "not found" in msg:
        return "not_found"
    if "429" in msg or "rate limit" in msg or "too many" in msg:
        return "rate_limit"
    if any(
        s in msg
        for s in (
            "timeout",
            "timed out",
            "econnreset",
            "enotfound",
            "network",
            "socket",
            "connection",
        )
    ):
        return "transient"
    return "transient"  # default: assume transient


def should_retry(cls: ErrorClass) -> bool:
    return cls in ("transient", "rate_limit")


def backoff_ms(attempt: int, cls: ErrorClass, *, rng: random.Random | None = None) -> float:
    base = DEFAULTS.BACKOFF_BASE_MS * 3 if cls == "rate_limit" else DEFAULTS.BACKOFF_BASE_MS
    exp = base * (2 ** (attempt - 1))
    r = rng if rng is not None else random
    jitter = r.random() * base * 0.3
    return min(exp + jitter, DEFAULTS.BACKOFF_MAX_MS)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = DEFAULTS.MAX_RETRIES,
    on_retry: OnRetry | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    last_error: BaseException = RuntimeError("unknown")
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except BaseException as exc:
            last_error = exc
            cls = classify_error(exc)
            if not should_retry(cls) or attempt == max_retries:
                raise
            wait_ms = backoff_ms(attempt, cls, rng=rng)
            if on_retry is not None:
                on_retry(attempt, exc, wait_ms)
            await sleep_fn(wait_ms / 1000.0)
    raise last_error
