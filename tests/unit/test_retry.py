"""Verify error classifier, backoff, and with_retry."""

from __future__ import annotations

import random

import pytest

from engine.resilience.retry import (
    backoff_ms,
    classify_error,
    should_retry,
    with_retry,
)


def test_classifier_recognises_categories() -> None:
    assert classify_error("HTTP 404 not found") == "not_found"
    assert classify_error("HTTP 429 rate limit exceeded") == "rate_limit"
    assert classify_error("Cloudflare turnstile required") == "blocked"
    assert classify_error("connection timeout") == "transient"
    assert classify_error("something weird") == "transient"  # default


def test_should_retry_only_for_transient_and_rate_limit() -> None:
    assert should_retry("transient")
    assert should_retry("rate_limit")
    assert not should_retry("blocked")
    assert not should_retry("not_found")
    assert not should_retry("permanent")


def test_backoff_grows_exponentially() -> None:
    rng = random.Random(0)
    a1 = backoff_ms(1, "transient", rng=rng)
    a3 = backoff_ms(3, "transient", rng=rng)
    assert a3 > a1


def test_rate_limit_backoff_is_larger() -> None:
    rng = random.Random(0)
    transient = backoff_ms(1, "transient", rng=rng)
    rl = backoff_ms(1, "rate_limit", rng=rng)
    assert rl > transient


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient() -> None:
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise OSError("connection timeout")
        return "ok"

    async def no_sleep(_s: float) -> None:
        return None

    out = await with_retry(flaky, sleep_fn=no_sleep)
    assert out == "ok"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_blocked() -> None:
    attempts: list[int] = []

    async def blocked() -> str:
        attempts.append(1)
        raise RuntimeError("turnstile required")

    async def no_sleep(_s: float) -> None:
        return None

    with pytest.raises(RuntimeError):
        await with_retry(blocked, sleep_fn=no_sleep)
    assert len(attempts) == 1
