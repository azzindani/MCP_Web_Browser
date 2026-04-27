"""Smoke tests. No network, no MCP imports, no Playwright.

Import and exercise every new module to confirm nothing is broken
before running the heavier integration / e2e suites.
"""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from engine.config.defaults import DEFAULTS
from engine.core.checkpoint import Checkpoint
from engine.core.queue import TaskQueue, make_task
from engine.core.timer import Timer
from engine.db.schema import init_schema
from engine.output.display import DisplaySink
from engine.output.stream import StreamWriter
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.tls import make_chrome_ssl_context


# ── config / defaults ──────────────────────────────────────────

def test_defaults_loads() -> None:
    assert DEFAULTS.HTTP_TIMEOUT > 0
    assert DEFAULTS.DB_PATH
    assert DEFAULTS.USER_AGENT


# ── resilience ────────────────────────────────────────────────

def test_circuit_breaker_allows_by_default() -> None:
    cb = CircuitBreaker()
    assert cb.allow("example.com")


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker()
    for _ in range(DEFAULTS.CIRCUIT_THRESHOLD):
        cb.failure("bad.com")
    assert not cb.allow("bad.com")


def test_rate_limiter_instantiates() -> None:
    rl = RateLimiter()
    assert rl is not None


# ── task queue ────────────────────────────────────────────────

def test_queue_enqueue_dequeue() -> None:
    q = TaskQueue()
    t = make_task("https://example.com", priority=1)
    q.enqueue(t)
    assert q.pending_count == 1
    out = q.dequeue()
    assert out is not None
    assert out.url == "https://example.com"
    assert q.pending_count == 0
    assert q.running_count == 1


def test_queue_dedup() -> None:
    q = TaskQueue()
    q.enqueue(make_task("https://example.com"))
    q.enqueue(make_task("https://example.com"))  # duplicate
    assert q.pending_count == 1


def test_queue_priority_order() -> None:
    q = TaskQueue()
    q.enqueue(make_task("https://a.com", priority=5))
    q.enqueue(make_task("https://b.com", priority=1))
    q.enqueue(make_task("https://c.com", priority=3))
    first = q.dequeue()
    assert first is not None
    assert first.url == "https://b.com"


def test_queue_mark_done() -> None:
    q = TaskQueue()
    t = make_task("https://x.com")
    q.enqueue(t)
    q.dequeue()
    q.mark_done(t.id)
    assert q.done_count == 1
    assert q.running_count == 0


def test_queue_dead_letter() -> None:
    q = TaskQueue()
    t = make_task("https://fail.com", max_retries=1)
    q.enqueue(t)
    q.dequeue()
    q.mark_failed(t.id, "timeout")
    # retries=1 < max_retries=1 means it gets requeued once, then dead letter
    t2 = q.dequeue()
    assert t2 is not None
    q.mark_failed(t2.id, "timeout again")
    assert q.dead_count == 1


def test_queue_snapshot_roundtrip() -> None:
    q = TaskQueue()
    q.enqueue(make_task("https://snap.com"))
    t = q.dequeue()
    assert t is not None
    q.mark_done(t.id)
    snap = q.snapshot()
    assert "https://snap.com" in snap["done_urls"]

    q2 = TaskQueue()
    q2.load_snapshot(snap)
    assert q2.is_done_url("https://snap.com")


# ── timer ────────────────────────────────────────────────────

def test_timer_phase_tracking() -> None:
    t = Timer("run_smoke")
    t.start_phase("http", total=5)
    t.tick("http", "http_json", "example.com", 120)
    t.tick("http", "http_json", "example.com", 200, error=True)
    t.end_phase("http")
    stats = t.get_stats()
    assert stats.phases["http"].completed == 2
    assert stats.phases["http"].errors == 1
    assert stats.by_mode["http_json"]["ok"] == 1
    assert stats.by_mode["http_json"]["err"] == 1


# ── display ──────────────────────────────────────────────────

def test_display_sink_writes_to_buffer() -> None:
    buf = io.StringIO()
    d = DisplaySink(buf)
    d.ok("test_label", "detail")
    d.err("bad_label")
    d.section("SECTION")
    out = buf.getvalue()
    assert "test_label" in out
    assert "bad_label" in out
    assert "SECTION" in out


# ── stream writer ─────────────────────────────────────────────

def test_stream_writer_append(tmp_path: Path) -> None:
    p = tmp_path / "out.jsonl"
    sw = StreamWriter(p)
    sw.write({"url": "https://x.com", "status": "ok"})
    sw.write({"url": "https://y.com", "status": "error"})
    sw.close()
    assert p.exists()
    records = sw.read_all()
    assert len(records) == 2
    assert records[0]["url"] == "https://x.com"


def test_stream_writer_context_manager(tmp_path: Path) -> None:
    p = tmp_path / "ctx.jsonl"
    with StreamWriter(p) as sw:
        sw.write({"n": 1})
    assert p.read_text().strip() != ""


# ── checkpoint ───────────────────────────────────────────────

def test_checkpoint_save_and_load(tmp_path: Path) -> None:
    cp = Checkpoint(tmp_path / "cp.json", run_id="r1")
    urls = {"https://a.com", "https://b.com"}
    cp.save(urls, total_count=2)
    cp2 = Checkpoint(tmp_path / "cp.json", run_id="r1")
    assert "https://a.com" in cp2.done_urls()
    assert "https://b.com" in cp2.done_urls()


# ── db schema ─────────────────────────────────────────────────

def test_schema_creates_core_tables() -> None:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for expected in ("pages", "stocks", "news", "files", "links", "domains", "fts_pages"):
        assert expected in tables, f"missing table: {expected}"


# ── TLS ────────────────────────────────────────────────────────

def test_chrome_ssl_context_builds() -> None:
    ctx = make_chrome_ssl_context()
    assert ctx is not None


# ── CI compliance gate ────────────────────────────────────────

def test_no_mcp_imports_in_engine() -> None:
    """engine/** and shared/** must not import from mcp.*"""
    import re
    repo_root = Path(__file__).parent.parent.parent
    pattern = re.compile(r"^(from|import)\s+mcp", re.MULTILINE)
    violations: list[str] = []
    for directory in ("engine", "shared"):
        for py in (repo_root / directory).rglob("*.py"):
            if pattern.search(py.read_text(encoding="utf-8")):
                violations.append(str(py.relative_to(repo_root)))
    assert violations == [], f"mcp.* imports found in engine/shared: {violations}"


def test_no_stdout_from_engine_modules() -> None:
    """Engine modules must not call print() — use stderr helpers."""
    import re
    repo_root = Path(__file__).parent.parent.parent / "engine"
    # Allow print(..., file=sys.stderr) but not bare print to stdout
    bare_print = re.compile(r"^\s*print\((?![^)]*file=sys\.stderr)", re.MULTILINE)
    violations: list[str] = []
    skip = {"cli.py"}  # CLI may print results to stdout intentionally
    for py in repo_root.rglob("*.py"):
        if py.name in skip:
            continue
        if bare_print.search(py.read_text(encoding="utf-8")):
            violations.append(str(py.relative_to(repo_root.parent)))
    assert violations == [], f"bare print() found in engine: {violations}"
