"""Verify CircuitBreaker state machine: closed → open → half_open → closed."""

from __future__ import annotations

from engine.config.defaults import DEFAULTS
from engine.resilience.circuit_breaker import CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.t = 0

    def now(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def test_closed_allows_initially() -> None:
    cb = CircuitBreaker(now_fn=FakeClock().now)
    assert cb.allow("example.com") is True
    assert cb.state_of("example.com") == "closed"


def test_opens_after_threshold_failures() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(now_fn=clock.now)
    for _ in range(DEFAULTS.CIRCUIT_THRESHOLD):
        cb.failure("flaky.com")
    assert cb.state_of("flaky.com") == "open"
    assert cb.allow("flaky.com") is False


def test_success_resets_to_closed() -> None:
    cb = CircuitBreaker(now_fn=FakeClock().now)
    for _ in range(2):
        cb.failure("x.com")
    cb.success("x.com")
    assert cb.state_of("x.com") == "closed"


def test_open_transitions_to_half_open_after_reset() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(now_fn=clock.now)
    for _ in range(DEFAULTS.CIRCUIT_THRESHOLD):
        cb.failure("flaky.com")
    assert cb.state_of("flaky.com") == "open"

    clock.advance(DEFAULTS.CIRCUIT_RESET_MS + 1)
    assert cb.allow("flaky.com") is True
    assert cb.state_of("flaky.com") == "half_open"


def test_stats_reports_failing_domains_only() -> None:
    cb = CircuitBreaker(now_fn=FakeClock().now)
    cb.allow("healthy.com")
    cb.failure("flaky.com")
    s = cb.stats()
    assert "flaky.com" in s
    assert "healthy.com" not in s
