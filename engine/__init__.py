"""Engine entry points used by `server.py`. No `mcp.*` imports here.

The thin server (`server.py`) imports a handful of coroutines from this
module and exposes each as a one-line MCP tool. State (httpx client,
breaker, limiter, sqlite connection) is held in a process-wide
`_Runtime` and constructed lazily so unit tests can run without opening
real connections.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlparse

import httpx

from engine.config.defaults import DEFAULTS
from engine.db.indexer import Indexer
from engine.db.query import QueryEngine
from engine.db.schema import init_schema
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.http_worker import HttpWorker, Task
from engine.workers.search_worker import SearchWorker
from shared.path_safety import resolve_path
from shared.platform_utils import get_inspect_chars
from shared.version_control import snapshot


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
