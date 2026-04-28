"""Batch scheduler. Orchestrates HTTP, browser, and crawl worker pools.

Mirrors core/scheduler.ts in krawl. The MCP server bypasses this and
calls engine entry-points directly (JIT, one tool call = one operation).
This class is used by the optional CLI (engine/cli.py) when running a
batch of tasks end-to-end.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from engine.config.defaults import DEFAULTS
from engine.core.checkpoint import Checkpoint
from engine.core.queue import Task, TaskQueue, make_task
from engine.core.router import Router
from engine.core.timer import Timer
from engine.db.indexer import Indexer
from engine.db.query import QueryEngine
from engine.db.schema import init_schema
from engine.output.stream import StreamWriter
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.crawl_worker import CrawlTask, CrawlWorker
from engine.workers.http_worker import HttpWorker
from engine.workers.http_worker import Task as HttpTask
from shared.path_safety import resolve_path


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SchedulerConfig:
    db_path: str = DEFAULTS.DB_PATH
    jsonl_path: str = DEFAULTS.JSONL_PATH
    checkpoint_path: str = "krawl_checkpoint.json"
    router_cache_path: str = "router_cache.json"
    http_concurrency: int = DEFAULTS.HTTP_CONCURRENCY
    browser_contexts: int = DEFAULTS.BROWSER_CONTEXTS
    instance_id: str = "default"
    resume: bool = False


class Scheduler:
    def __init__(self, cfg: SchedulerConfig | None = None) -> None:
        self._cfg = cfg or SchedulerConfig()
        self._run_id = f"run_{int(time.time())}_{self._cfg.instance_id}"

        db_path = resolve_path(self._cfg.db_path)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        init_schema(self._db)

        self._breaker  = CircuitBreaker()
        self._limiter  = RateLimiter()
        self._queue    = TaskQueue()
        self._router   = Router(self._cfg.router_cache_path)
        self._timer    = Timer(self._run_id)
        self._indexer  = Indexer(self._db)
        self._stream   = StreamWriter(self._cfg.jsonl_path)
        self._checkpoint = Checkpoint(
            self._cfg.checkpoint_path,
            run_id=self._run_id,
            config={k: str(v) for k, v in vars(self._cfg).items()},
        )

    # ── public API ───────────────────────────────────────────────────

    def add_tasks(self, inputs: list[dict[str, Any]]) -> None:
        done_urls = self._checkpoint.done_urls() if self._cfg.resume else set()
        skipped = 0
        for inp in inputs:
            url = inp.get("url")
            if not url or url in done_urls:
                skipped += 1
                continue
            self._queue.enqueue(
                make_task(
                    url=url,
                    name=inp.get("name") or "",
                    mode=inp.get("mode") or "auto",
                    priority=int(inp.get("priority") or 5),
                    group=inp.get("group") or "default",
                    tags=list(inp.get("tags") or []),
                    max_retries=int(inp.get("max_retries") or DEFAULTS.MAX_RETRIES),
                    crawl_depth=inp.get("crawl_depth"),
                )
            )
        msg = f"Tasks: {self._queue.pending_count} pending"
        if skipped:
            msg += f" ({skipped} skipped from checkpoint)"
        _stderr(msg)

    async def run(self) -> None:
        total = self._queue.pending_count
        if total == 0:
            _stderr("No tasks to run.")
            return

        _stderr("=" * 60)
        _stderr(f"KRAWL ENGINE — run {self._run_id}")
        _stderr("=" * 60)
        _stderr(f"Total tasks : {total}")
        _stderr(f"Instance    : {self._cfg.instance_id}")
        _stderr(f"Database    : {self._cfg.db_path}")
        _stderr(f"Output      : {self._cfg.jsonl_path}")

        with self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, started_at, total_tasks, instance_id, config)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._run_id, _now_iso(), total, self._cfg.instance_id, "{}"),
            )

        self._timer.start_phase("total", total)
        await self._resolve_auto_modes()
        await self._run_all_pools()
        await self._finalize(total)

    # ── phases ─────────────────────────────────────────────────────

    async def _resolve_auto_modes(self) -> None:
        if self._queue.pending_count == 0:
            return
        all_tasks = self._queue.drain_pending()
        auto = [t for t in all_tasks if t.mode == "auto"]
        rest = [t for t in all_tasks if t.mode != "auto"]
        if auto:
            _stderr(f"Resolving mode for {len(auto)} auto tasks...")
            sem = asyncio.Semaphore(10)

            async def _resolve(task: Task) -> Task:
                async with sem:
                    task.mode = await self._router.resolve(task.url)
                return task

            resolved = await asyncio.gather(*[_resolve(t) for t in auto])
            rest.extend(resolved)
        for t in rest:
            t.status = "pending"
            self._queue.enqueue(t)

    async def _run_all_pools(self) -> None:
        tasks: list[Task] = []
        while self._queue.pending_count > 0:
            t = self._queue.dequeue()
            if t:
                tasks.append(t)

        http_tasks   = [t for t in tasks if t.mode in ("http_json", "http_curl")]
        browser_tasks = [t for t in tasks if t.mode == "browser"]
        crawl_tasks  = [t for t in tasks if t.mode == "crawl"]
        blocked      = [t for t in tasks if t.mode == "blocked"]

        if blocked:
            _stderr(f"{len(blocked)} tasks blocked (Turnstile / hardcoded)")
        _stderr(
            f"Routing: HTTP={len(http_tasks)} Browser={len(browser_tasks)}"
            f" Crawl={len(crawl_tasks)} Blocked={len(blocked)}"
        )

        client     = HttpWorker.make_client()
        http_worker  = HttpWorker(client, self._breaker, self._limiter)
        crawl_worker = CrawlWorker(client, self._breaker, self._limiter)

        try:
            http_results = await self._run_http_phase(http_tasks, http_worker)
            for r in http_results:
                self._process_http_result(r)

            # Browser phase: import lazily so tests can skip playwright
            if browser_tasks:
                from engine.workers.browser_worker import BrowserWorker
                bw = BrowserWorker(self._breaker, self._limiter)
                browser_results = await self._run_browser_phase(browser_tasks, bw)
                for r in browser_results:
                    self._process_http_result(r)

            for ct in crawl_tasks:
                report = await crawl_worker.run(
                    CrawlTask(
                        url=ct.url,
                        crawl_depth=ct.crawl_depth or DEFAULTS.MAX_CRAWL_DEPTH,
                        max_pages=DEFAULTS.MAX_CRAWL_PAGES,
                    ),
                    checkpoint=self._checkpoint,
                )
                for page in report.pages:
                    self._process_dict(vars(page))
        finally:
            await client.aclose()

    async def _run_http_phase(
        self, tasks: list[Task], worker: HttpWorker
    ) -> list[Any]:
        if not tasks:
            return []
        _stderr(f"─── HTTP PHASE ({len(tasks)} tasks) ───")
        self._timer.start_phase("http", len(tasks))
        sem = asyncio.Semaphore(self._cfg.http_concurrency)

        async def _fetch(t: Task) -> Any:
            async with sem:
                return await worker.fetch_one(
                    HttpTask(url=t.url, name=t.name, mode=t.mode, group=t.group)
                )

        results = await asyncio.gather(*[_fetch(t) for t in tasks])
        for r in results:
            domain = urlparse(r.url).hostname or r.url
            self._timer.tick("http", r.mode, domain, r.elapsed_ms, r.status != "ok")
        self._timer.end_phase("http")
        self._timer.display()
        return list(results)

    async def _run_browser_phase(
        self, tasks: list[Task], worker: Any
    ) -> list[Any]:
        if not tasks:
            return []
        _stderr(f"─── BROWSER PHASE ({len(tasks)} tasks) ───")
        self._timer.start_phase("browser", len(tasks))
        results = await worker.run_many(tasks)
        for r in results:
            domain = urlparse(r.url).hostname or r.url
            self._timer.tick("browser", "browser", domain, r.elapsed_ms, r.status != "ok")
        self._timer.end_phase("browser")
        self._timer.display()
        return results

    # ── result processing ──────────────────────────────────────────

    def _process_http_result(self, r: Any) -> None:
        d = vars(r) if not isinstance(r, dict) else r
        # strip the Task object — not JSON-serialisable
        d = {k: v for k, v in d.items() if k != "task"}
        self._process_dict(d)
        if hasattr(r, "task") and r.task:
            self._queue.mark_done(r.task.id)

    def _process_dict(self, d: dict[str, Any]) -> None:
        self._stream.write(d)
        self._indexer.index(d, self._run_id)
        if (
            DEFAULTS.CHECKPOINT_EVERY > 0
            and self._queue.done_count % DEFAULTS.CHECKPOINT_EVERY == 0
        ):
            self._checkpoint.save(
                self._queue.get_done_urls(), self._queue.total_count
            )

    # ── finalize ──────────────────────────────────────────────────

    async def _finalize(self, total: int) -> None:
        self._timer.end_phase("total")
        self._checkpoint.save(self._queue.get_done_urls(), total)
        self._stream.close()
        with self._db:
            self._db.execute(
                "UPDATE runs SET finished_at=?, completed=?, errors=? WHERE run_id=?",
                (
                    _now_iso(),
                    self._queue.done_count,
                    self._queue.dead_count,
                    self._run_id,
                ),
            )
        self._timer.summary()
        dead = self._queue.get_dead_letter()
        if dead:
            _stderr(f"\nDead letter ({len(dead)} tasks):")
            for t in dead:
                _stderr(f"  - {t.name}: {t.error}")

    # ── accessors ────────────────────────────────────────────────

    def get_db(self) -> sqlite3.Connection:
        return self._db

    def get_indexer(self) -> Indexer:
        return self._indexer

    def get_query(self) -> QueryEngine:
        return QueryEngine(self._db)
