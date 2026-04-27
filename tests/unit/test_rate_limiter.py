"""Verify RateLimiter token-bucket semantics with injected clock."""

from __future__ import annotations

import asyncio

import pytest

from engine.resilience.rate_limiter import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.t = 0

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@pytest.mark.asyncio
async def test_first_acquire_is_immediate() -> None:
    clock = FakeClock()

    async def no_sleep(_seconds: float) -> None:
        return None

    rl = RateLimiter(now_fn=clock.now, sleep_fn=no_sleep)
    await rl.acquire("example.com")  # initial bucket has burst capacity


@pytest.mark.asyncio
async def test_blocks_when_bucket_empty_until_refill() -> None:
    clock = FakeClock()
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock.advance(int(seconds * 1000))

    rl = RateLimiter(now_fn=clock.now, sleep_fn=fake_sleep)
    # Default rps = 0.5, max_tokens = 1.0. Drain it.
    await rl.acquire("unknown-host.example")
    # Next acquire must wait for refill — should call sleep at least once.
    await rl.acquire("unknown-host.example")
    assert sleep_calls, "second acquire should have slept for refill"


def test_stats_smoke() -> None:
    rl = RateLimiter()
    asyncio.run(rl.acquire("example.com"))
    s = rl.stats()
    assert "example.com" in s
    assert s["example.com"]["rps"] > 0
