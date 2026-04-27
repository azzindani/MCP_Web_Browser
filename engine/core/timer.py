"""Performance tracking, ETA calculation, live display.

Mirrors core/timer.ts in krawl. All display goes to stderr so MCP
stdio framing is never corrupted. display() writes a single overwrite
line; summary() prints the full end-of-run report.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _fmt_ms(ms: float) -> str:
    s = int(ms / 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


@dataclass
class PhaseStats:
    name: str
    start_ms: float
    end_ms: float | None = None
    completed: int = 0
    total: int = 0
    errors: int = 0


@dataclass
class RunStats:
    run_id: str
    start_ms: float = field(default_factory=lambda: time.monotonic() * 1000)
    phases: dict[str, PhaseStats] = field(default_factory=dict)
    by_mode: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_domain: dict[str, dict[str, Any]] = field(default_factory=dict)


class Timer:
    def __init__(self, run_id: str) -> None:
        self._stats = RunStats(run_id=run_id)

    def start_phase(self, name: str, total: int) -> None:
        self._stats.phases[name] = PhaseStats(
            name=name,
            start_ms=time.monotonic() * 1000,
            total=total,
        )

    def tick(
        self,
        phase: str,
        mode: str,
        domain: str,
        elapsed_ms: int,
        error: bool = False,
    ) -> None:
        p = self._stats.phases.get(phase)
        if p:
            p.completed += 1
            if error:
                p.errors += 1

        bm = self._stats.by_mode.setdefault(mode, {"ok": 0, "err": 0, "total_ms": 0})
        bm["err" if error else "ok"] += 1
        bm["total_ms"] += elapsed_ms

        bd = self._stats.by_domain.setdefault(domain, {"count": 0, "total_ms": 0})
        bd["count"] += 1
        bd["total_ms"] += elapsed_ms

    def end_phase(self, name: str) -> None:
        p = self._stats.phases.get(name)
        if p:
            p.end_ms = time.monotonic() * 1000

    def _totals(self) -> tuple[int, int, int]:
        completed = total = errors = 0
        for p in self._stats.phases.values():
            completed += p.completed
            total += p.total
            errors += p.errors
        return completed, total, errors

    def display(self) -> None:
        completed, total, errors = self._totals()
        wall_ms = time.monotonic() * 1000 - self._stats.start_ms
        wall_sec = wall_ms / 1000
        rate = completed / wall_sec if wall_sec > 0 else 0
        remaining = total - completed
        eta_sec = remaining / rate if rate > 0 else 0
        pct = completed / total if total > 0 else 0
        filled = min(30, max(0, round(pct * 30)))
        bar = "█" * filled + "░" * (30 - filled)
        mode_s = "  ".join(
            f"{m}✓{s['ok']}✗{s['err']}" for m, s in self._stats.by_mode.items()
        )
        line = (
            f"[{_fmt_ms(wall_ms)}] {bar} {round(pct * 100)}%"
            f"  {completed}/{total}"
            f"  {rate:.1f}/s"
            f"  ETA:{_fmt_ms(eta_sec * 1000)}"
            f"  {mode_s}"
            f"  err:{errors}"
        )
        sys.stderr.write("\r" + line)
        sys.stderr.flush()

    def summary(self) -> None:
        completed, total, errors = self._totals()
        wall_ms = time.monotonic() * 1000 - self._stats.start_ms
        rate = completed / (wall_ms / 1000) if wall_ms > 0 else 0
        _stderr("\n" + "=" * 60)
        _stderr("RUN SUMMARY")
        _stderr("=" * 60)
        _stderr(f"Run ID     : {self._stats.run_id}")
        _stderr(f"Total time : {_fmt_ms(wall_ms)}")
        _stderr(f"Completed  : {completed}/{total}")
        _stderr(f"Errors     : {errors}")
        _stderr(f"Avg rate   : {rate:.2f} tasks/sec")
        _stderr("\nBy mode:")
        for mode, s in self._stats.by_mode.items():
            n = s["ok"] + s["err"]
            avg = s["total_ms"] / n if n else 0
            _stderr(f"  {mode:<14} ok={s['ok']}  err={s['err']}  avg={avg/1000:.2f}s")
        _stderr("\nTop domains:")
        for domain, s in sorted(
            self._stats.by_domain.items(), key=lambda kv: -kv[1]["count"]
        )[:8]:
            avg = s["total_ms"] / s["count"]
            _stderr(f"  {domain:<35} {s['count']} reqs  avg={avg/1000:.2f}s")
        _stderr("\nPhases:")
        for p in self._stats.phases.values():
            dur = (p.end_ms or time.monotonic() * 1000) - p.start_ms
            _stderr(
                f"  {p.name:<20} {p.completed}/{p.total}"
                f"  {_fmt_ms(dur)}  err={p.errors}"
            )

    def get_stats(self) -> RunStats:
        return self._stats
