"""Per-domain circuit breaker. Prevents hammering failing sites.

States: closed → (N consecutive failures) → open → (reset delay) →
half_open → (one success) → closed.

Time source is injectable via `now_fn` so tests don't need `time.sleep`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from engine.config.defaults import DEFAULTS

CircuitState = Literal["closed", "open", "half_open"]


@dataclass
class _DomainCircuit:
    state: CircuitState = "closed"
    failures: int = 0
    last_failure_ms: int = 0
    last_success_ms: int = 0


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class CircuitBreaker:
    def __init__(self, now_fn: Callable[[], int] = _now_ms) -> None:
        self._circuits: dict[str, _DomainCircuit] = {}
        self._now = now_fn

    def _circuit(self, domain: str) -> _DomainCircuit:
        c = self._circuits.get(domain)
        if c is None:
            c = _DomainCircuit(last_success_ms=self._now())
            self._circuits[domain] = c
        return c

    def allow(self, domain: str) -> bool:
        c = self._circuit(domain)
        now = self._now()
        if c.state == "closed":
            return True
        if c.state == "open":
            if now - c.last_failure_ms > DEFAULTS.CIRCUIT_RESET_MS:
                c.state = "half_open"
                return True
            return False
        # half_open — allow one probe through.
        return True

    def success(self, domain: str) -> None:
        c = self._circuit(domain)
        c.failures = 0
        c.last_success_ms = self._now()
        c.state = "closed"

    def failure(self, domain: str) -> None:
        c = self._circuit(domain)
        c.failures += 1
        c.last_failure_ms = self._now()
        if c.failures >= DEFAULTS.CIRCUIT_THRESHOLD:
            c.state = "open"

    def state_of(self, domain: str) -> CircuitState:
        return self._circuit(domain).state

    def stats(self) -> dict[str, dict[str, int | str]]:
        out: dict[str, dict[str, int | str]] = {}
        for domain, c in self._circuits.items():
            if c.failures or c.state != "closed":
                out[domain] = {"state": c.state, "failures": c.failures}
        return out
