"""Engine entry points used by `server.py`. No `mcp.*` imports here."""

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
from shared.handover import next_step
from shared.path_safety import resolve_path
from shared.platform_utils import (
    get_inspect_chars,
    get_max_depth,
    get_max_pages,
    get_max_results,
    get_max_rows,
)
from shared.progress import fail, info, ok, warn
from shared.receipt import append_receipt
from shared.version_control import atomic_write_text, snapshot


def _tok(d: Any) -> int:
    return len(str(d)) // 4


class _Runtime:
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
    global _RT
    _RT = None
