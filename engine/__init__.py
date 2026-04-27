"""Engine entry points used by `server.py`. No `mcp.*` imports here.

The thin server (`server.py`) imports a handful of coroutines from this
module and exposes each as a one-line MCP tool. State (httpx client,
breaker, limiter, sqlite connection) is held in a process-wide
`_Runtime` and constructed lazily so unit tests can run without opening
real connections.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from engine.config.defaults import DEFAULTS
from engine.core.checkpoint import Checkpoint
from engine.db.indexer import Indexer
from engine.db.query import STATS_TABLES, QueryEngine
from engine.db.schema import init_schema
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.crawl_worker import CrawlTask, CrawlWorker
from engine.workers.http_worker import HttpWorker, Task
from engine.workers.search_worker import SearchWorker
from shared.path_safety import resolve_path
from shared.platform_utils import (
    get_inspect_chars,
    get_max_depth,
    get_max_pages,
    get_max_rows,
)
from shared.version_control import atomic_write_text, snapshot


class _Runtime:
    """Lazy-init container for shared engine state."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._breaker = CircuitBreaker()
        self._limiter = RateLimiter()
        self._db: sqlite3.Connection | None = None

    def http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = HttpWorker.make_client()
        return self._client

    def http_worker(self) -> HttpWorker:
        return HttpWorker(self.http_client(), self._breaker, self._limiter)

    def search_worker(self) -> SearchWorker:
        return SearchWorker(self.http_client(), self._breaker, self._limiter)

    def db(self) -> sqlite3.Connection:
        if self._db is None:
            path = resolve_path(DEFAULTS.DB_PATH)
            snapshot(path)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            init_schema(conn)
            self._db = conn
        return self._db

    def indexer(self) -> Indexer:
        return Indexer(self.db())

    def query(self) -> QueryEngine:
        return QueryEngine(self.db())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._db is not None:
            self._db.close()
            self._db = None

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "breaker": self._breaker.stats(),
            "limiter": self._limiter.stats(),
            "client_open": self._client is not None,
            "db_open": self._db is not None,
        }


_RT: _Runtime | None = None


def runtime() -> _Runtime:
    global _RT
    if _RT is None:
        _RT = _Runtime()
    return _RT


def reset_runtime() -> None:
    """Test-only: drop the singleton so the next `runtime()` rebuilds."""
    global _RT
    _RT = None


# ── Public entry points ────────────────────────────────────────────────


async def search_web(query: str, limit: int | None = None) -> dict[str, Any]:
    rt = runtime()
    result = await rt.search_worker().search(query, limit=limit)
    return {
        "ok": result.backend != "none",
        "query": result.query,
        "backend": result.backend,
        "hits": [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet[:200],
                "backend": h.backend,
            }
            for h in result.hits
        ],
        "total": result.total,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
    }


async def fetch_one(url: str, run_id: str = "mcp") -> dict[str, Any]:
    rt = runtime()
    result = await rt.http_worker().fetch_one(Task(url=url))
    indexed: list[str] = []
    if result.status == "ok":
        report = rt.indexer().index(
            {
                "url": result.url,
                "title": result.title,
                "status": result.status,
                "mode": result.mode,
                "elapsed_ms": result.elapsed_ms,
                "extracted": result.extracted,
                "group": result.group,
                "ticker": result.ticker,
                "extractedAt": result.extracted_at,
            },
            run_id=run_id,
        )
        indexed = report.indexed
    return {
        "ok": result.status == "ok",
        "url": result.url,
        "mode": result.mode,
        "status": result.status,
        "elapsed_ms": result.elapsed_ms,
        "indexed": indexed,
        "error": result.error,
    }


async def inspect_one(url: str) -> dict[str, Any]:
    rt = runtime()
    result = await rt.http_worker().fetch_one(Task(url=url))
    cap = get_inspect_chars()
    head = ""
    if result.status == "ok" and isinstance(result.extracted, dict):
        head = str(result.extracted.get("text_preview") or "")
        if not head:
            head = str(result.extracted)
    return {
        "ok": result.status == "ok",
        "url": result.url,
        "title": result.title,
        "status": result.status,
        "head": head[:cap],
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
    }


async def probe_one(url: str) -> dict[str, Any]:
    """LOCATE: detect mode without persisting. HEAD probe + content-type sniff."""
    rt = runtime()
    domain = urlparse(url).hostname or url
    if not rt._breaker.allow(domain):
        return {"ok": False, "url": url, "domain": domain, "mode": "blocked",
                "error": "circuit open"}
    try:
        response = await rt.http_client().head(
            url, follow_redirects=True, timeout=5.0
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "url": url, "domain": domain, "mode": "error",
                "error": str(exc)[:80]}
    ct = response.headers.get("content-type", "").lower()
    if "json" in ct:
        mode = "http_json"
    elif "html" in ct:
        mode = "http_curl"
    else:
        mode = "http_curl"
    return {
        "ok": True,
        "url": url,
        "domain": domain,
        "mode": mode,
        "status_code": response.status_code,
        "content_type": ct,
    }


def verify_one(url: str) -> dict[str, Any]:
    rt = runtime()
    rows = rt.query().select(
        "SELECT * FROM pages WHERE url = ? LIMIT 1", (url,), limit=1
    )
    if not rows:
        return {"ok": False, "url": url, "error": "not_indexed"}
    row = rows[0]
    return {
        "ok": True,
        "url": row["url"],
        "title": row["title"],
        "domain": row["domain"],
        "status": row["status"],
        "mode": row["mode"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


def engine_status() -> dict[str, Any]:
    return runtime().status()


# ── Query tier ─────────────────────────────────────────────────────────


def query_locate() -> dict[str, Any]:
    """List tables + row counts. Surgical."""
    s = runtime().query().stats()
    tables = {k: v for k, v in s.items() if k != "db_bytes"}
    return {"ok": True, "tables": tables, "db_bytes": s.get("db_bytes", 0)}


def query_search(
    query: str, table: str = "fts_pages", limit: int | None = None
) -> dict[str, Any]:
    rt = runtime()
    cap = limit if limit is not None else get_max_rows()
    try:
        rows = rt.query().search(query, table=table, limit=cap)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "table must be fts_pages|fts_news|fts_files"}
    return {
        "ok": True,
        "table": table,
        "query": query,
        "rows": rows,
        "total": len(rows),
        "truncated": len(rows) >= cap,
    }


def query_select(
    sql: str, params: tuple[Any, ...] = (), limit: int | None = None
) -> dict[str, Any]:
    rt = runtime()
    cap = limit if limit is not None else get_max_rows()
    try:
        rows = rt.query().select(sql, params=params, limit=cap)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "only SELECT/WITH allowed"}
    return {
        "ok": True,
        "rows": rows,
        "total": len(rows),
        "truncated": len(rows) >= cap,
    }


def query_export(table: str, out_path: str, fmt: str = "csv") -> dict[str, Any]:
    if fmt not in ("csv", "json"):
        return {"ok": False, "error": "bad_format", "hint": "fmt must be csv|json"}
    if table not in STATS_TABLES:
        return {"ok": False, "error": "unknown_table", "hint": f"table not in {sorted(STATS_TABLES)}"}
    rt = runtime()
    try:
        if fmt == "csv":
            text = rt.query().to_csv(table)
        else:
            rows = rt.query().select(
                f"SELECT * FROM {table}",  # noqa: S608 — whitelisted above
                limit=100_000,
            )
            text = json.dumps(rows, indent=2, default=str)
    except (ValueError, sqlite3.OperationalError) as exc:
        return {"ok": False, "error": str(exc)[:80], "hint": "export_failed"}

    target = resolve_path(out_path)
    snapshot(target)
    atomic_write_text(target, text)
    return {"ok": True, "path": str(target), "bytes": len(text)}


def query_stats() -> dict[str, Any]:
    s = runtime().query().stats()
    return {"ok": True, **s}


# ── Crawl tier ─────────────────────────────────────────────────────────


def _crawl_worker() -> CrawlWorker:
    rt = runtime()
    return CrawlWorker(rt.http_client(), rt._breaker, rt._limiter)


async def crawl_locate(url: str) -> dict[str, Any]:
    """LOCATE: same as browse_locate but returns crawl-specific hints."""
    base = await probe_one(url)
    base["base_path"] = urlparse(url).path or "/"
    return base


async def crawl_plan(url: str, max_links: int = 25) -> dict[str, Any]:
    """INSPECT (dry-run): fetch the seed once and list the would-be frontier."""
    rt = runtime()
    worker = _crawl_worker()
    task = CrawlTask(url=url, crawl_depth=1, max_pages=1)
    report = await worker.run(task)
    if not report.pages:
        return {"ok": False, "error": "fetch_failed"}
    seed = report.pages[0]
    return {
        "ok": seed.status == "ok",
        "url": seed.url,
        "title": seed.title,
        "links": seed.links[:max_links],
        "files": seed.files[:max_links],
        "elapsed_ms": seed.elapsed_ms,
    }


async def crawl_run(
    url: str,
    max_pages: int | None = None,
    max_depth: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    rt = runtime()
    pages_cap = max_pages if max_pages is not None else get_max_pages()
    depth_cap = max_depth if max_depth is not None else get_max_depth()
    rid = run_id or f"crawl-{int(time.time())}"
    cp = Checkpoint(f"krawl_checkpoint_{rid}.json", run_id=rid)

    worker = _crawl_worker()
    report = await worker.run(
        CrawlTask(url=url, crawl_depth=depth_cap, max_pages=pages_cap),
        checkpoint=cp,
    )
    indexer = rt.indexer()
    for page in report.pages:
        if page.status != "ok":
            continue
        indexer.index(
            {
                "url": page.url, "title": page.title,
                "status": page.status, "mode": page.mode,
                "elapsed_ms": page.elapsed_ms,
                "extracted": page.extracted, "group": page.group,
                "links": page.links,
                "extractedAt": page.extracted_at,
            },
            run_id=rid,
        )
    return {
        "ok": report.errors < len(report.pages),
        "run_id": report.run_id,
        "seed_url": report.seed_url,
        "pages": len(report.pages),
        "errors": report.errors,
        "files_discovered": report.files_discovered,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
    }


async def crawl_resume(
    run_id: str, url: str, max_pages: int | None = None
) -> dict[str, Any]:
    return await crawl_run(url, max_pages=max_pages, run_id=run_id)


def crawl_verify(run_id: str) -> dict[str, Any]:
    """Return per-run summary from task_log + pages joined."""
    rt = runtime()
    rows = rt.query().select(
        "SELECT COUNT(*) AS pages, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
        "FROM task_log WHERE run_id = ?",
        params=(run_id,),
        limit=1,
    )
    if not rows:
        return {"ok": False, "run_id": run_id, "error": "not_found"}
    row = rows[0]
    return {
        "ok": True,
        "run_id": run_id,
        "pages": int(row["pages"] or 0),
        "errors": int(row["errors"] or 0),
    }
