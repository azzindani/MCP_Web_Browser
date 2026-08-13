"""Engine entry points used by `server.py`. No `mcp.*` imports here."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import zoneinfo
from datetime import UTC, datetime
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
from engine.workers.browser_worker import BrowserTask, BrowserWorker
from engine.workers.crawl_worker import CrawlTask, CrawlWorker
from engine.workers.http_worker import HttpWorker, Task
from engine.workers.search_worker import SearchHit, SearchWorker
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
        self._browser_worker: BrowserWorker | None = None
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

    async def browser_search_worker(self) -> BrowserWorker:
        if self._browser_worker is None:
            self._browser_worker = await BrowserWorker.launch(self._breaker, self._limiter)
        return self._browser_worker

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
        if self._browser_worker is not None:
            await self._browser_worker.close()
            self._browser_worker = None
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

# Per-query failure counter — reset when the server restarts.
# Prevents models from looping the same failing query indefinitely.
_SEARCH_FAIL_COUNTS: dict[str, int] = {}
_SEARCH_FAIL_LIMIT = 2
_RESEARCH_PREVIEW = 800  # chars per source preview in research mode


def runtime() -> _Runtime:
    global _RT
    if _RT is None:
        _RT = _Runtime()
    return _RT


def reset_runtime() -> None:
    global _RT
    _RT = None


async def warmup_browser() -> None:
    """Pre-warm Chromium at startup. Errors are silently ignored."""
    try:
        await runtime().browser_search_worker()
    except Exception:  # noqa: BLE001
        pass


async def shutdown() -> None:
    """Clean shutdown: close browser, http client, db."""
    if _RT is not None:
        await _RT.aclose()


# ── Browse tier ────────────────────────────────────────────────────────────


def get_datetime() -> dict[str, Any]:
    utc_now = datetime.now(UTC)
    try:
        tz = zoneinfo.ZoneInfo(DEFAULTS.TIMEZONE)
        local_now = datetime.now(tz)
    except zoneinfo.ZoneInfoNotFoundError:
        local_now = utc_now
    date_str = local_now.strftime("%Y-%m-%d")
    return {
        "ok": True,
        "op": "browse_datetime",
        "utc_iso": utc_now.isoformat(timespec="seconds"),
        "local_iso": local_now.isoformat(timespec="seconds"),
        "date": date_str,
        "time": local_now.strftime("%H:%M:%S"),
        "day_of_week": local_now.strftime("%A"),
        "year": local_now.year,
        "month": local_now.month,
        "timezone": DEFAULTS.TIMEZONE,
        "hint": f"Today is {local_now.strftime('%A')}, {date_str}. Use this for date-aware queries.",
        "token_estimate": 60,
    }


async def search_web(query: str, limit: int | None = None) -> dict[str, Any]:
    # Hard loop-breaker: return terminal error after _SEARCH_FAIL_LIMIT consecutive
    # failures for the same query so models cannot spin indefinitely.
    query_key = query.lower().strip()
    if _SEARCH_FAIL_COUNTS.get(query_key, 0) >= _SEARCH_FAIL_LIMIT:
        _SEARCH_FAIL_COUNTS.pop(query_key, None)
        return {
            "ok": False,
            "op": "browse_search_exhausted",
            "error": "search_exhausted",
            "query": query,
            "hint": (
                f"browse_search has already failed {_SEARCH_FAIL_LIMIT} times for this query. "
                "Web search is unavailable. Answer from your training data instead. "
                "Do NOT call browse_search again."
            ),
        }

    rt = runtime()
    result = await rt.search_worker().search(query, limit=limit)
    success = result.backend != "none"
    progress = [
        ok("Web search", f"{result.total} hits via {result.backend}")
        if success
        else fail("Web search", f"all backends failed for: {query}")
    ]
    try:
        _tz = zoneinfo.ZoneInfo(DEFAULTS.TIMEZONE)
        _now = datetime.now(_tz)
    except zoneinfo.ZoneInfoNotFoundError:
        _now = datetime.now(UTC)
    _date_str = _now.strftime("%Y-%m-%d")
    res: dict[str, Any] = {
        "ok": success,
        "op": "browse_search",
        "query": result.query,
        "backend": result.backend,
        "hits": [
            {"title": h.title, "url": h.url, "snippet": h.snippet[:200], "backend": h.backend} for h in result.hits
        ],
        "total": result.total,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
        "current_date": _date_str,
        "current_year": _now.year,
        "date_hint": f"Today is {_now.strftime('%A')}, {_date_str}. Prefer sources from {_now.year}.",
        "progress": progress,
    }
    if not success:
        # Browser fallback: launch Chromium now if not already running.
        # Safe to do here — the MCP handshake is complete, so stdio won't be corrupted.
        _BROWSER_SEARCH_TIMEOUT = 20.0  # per-engine hard cap
        _BROWSER_LAUNCH_TIMEOUT = 30.0
        cap = get_max_results() if limit is None else limit
        browser_errors: list[str] = []
        bw = None
        try:
            bw = await asyncio.wait_for(rt.browser_search_worker(), timeout=_BROWSER_LAUNCH_TIMEOUT)
        except TimeoutError:
            browser_errors.append("browser_launch: timed out after 30s")
        except Exception as exc:  # noqa: BLE001
            browser_errors.append(f"browser_launch: {type(exc).__name__}: {exc}")
        for engine_name, search_fn in (
            ("browser_google", bw.search_google if bw else None),
            ("browser_ddg", bw.search_ddg if bw else None),
        ):
            if search_fn is None:
                continue
            try:
                raw_hits = await asyncio.wait_for(search_fn(query, cap), timeout=_BROWSER_SEARCH_TIMEOUT)
                if not raw_hits:
                    browser_errors.append(f"{engine_name}: 0 results (captcha or bot-block)")
                if raw_hits:
                    res["ok"] = True
                    res["backend"] = engine_name
                    res["hits"] = [
                        {"title": h["title"], "url": h["url"], "snippet": h["snippet"][:200], "backend": engine_name}
                        for h in raw_hits
                    ]
                    res["total"] = len(raw_hits)
                    cite_hints = [f"[{i + 1}] {h['title']} — {h['url']}" for i, h in enumerate(raw_hits)]
                    res["cite_hints"] = cite_hints
                    res["suggested_next"] = [
                        {"tool": "browse_fetch", "url": h["url"], "reason": f"[{i + 1}] {h['title'][:55]}"}
                        for i, h in enumerate(raw_hits[:3])
                    ] + [next_step("browse_research", "auto search+fetch for deep research with citations")]
                    res["carry_forward"] = {"urls": [h["url"] for h in raw_hits[:5]], "query": query}
                    res["handover"] = {
                        "instruction": (
                            "Call browse_fetch on the most relevant URLs above to get full content, "
                            "then synthesize your answer. "
                            "End your response with a '## Sources' section using the cite_format below."
                        ),
                        "cite_format": "## Sources\n" + "\n".join(f"- {c}" for c in cite_hints[:10]),
                    }
                    _SEARCH_FAIL_COUNTS.pop(query_key, None)
                    res["token_estimate"] = _tok(res)
                    return res
            except TimeoutError:
                browser_errors.append(f"{engine_name}: timeout after {_BROWSER_SEARCH_TIMEOUT}s")
            except Exception as exc:  # noqa: BLE001
                browser_errors.append(f"{engine_name}: {type(exc).__name__}: {exc}")

        _SEARCH_FAIL_COUNTS[query_key] = _SEARCH_FAIL_COUNTS.get(query_key, 0) + 1
        res["backend_errors"] = result.errors + browser_errors
        res["do_not_retry"] = True
        res["hint"] = (
            "All search backends failed. STOP — do not call browse_search again. "
            "Either answer from your training data or call browse_status to diagnose."
        )
        res["suggested_next"] = [next_step("browse_status", "check engine health")]
    else:
        _SEARCH_FAIL_COUNTS.pop(query_key, None)
        cite_hints = [f"[{i + 1}] {h.title} — {h.url}" for i, h in enumerate(result.hits)]
        res["cite_hints"] = cite_hints
        res["suggested_next"] = [
            {"tool": "browse_fetch", "url": h.url, "reason": f"[{i + 1}] {h.title[:55]}"}
            for i, h in enumerate(result.hits[:3])
        ] + [next_step("browse_research", "auto search+fetch for deep research with citations")]
        res["carry_forward"] = {"urls": [h.url for h in result.hits[:5]], "query": query}
        res["handover"] = {
            "instruction": (
                "Call browse_fetch on the most relevant URLs above to get full content, "
                "then synthesize your answer. "
                "End your response with a '## Sources' section using the cite_format below."
            ),
            "cite_format": "## Sources\n" + "\n".join(f"- {c}" for c in cite_hints[:10]),
        }
    res["token_estimate"] = _tok(res)
    return res


async def fetch_one(url: str, run_id: str = "mcp") -> dict[str, Any]:
    rt = runtime()
    result = await rt.http_worker().fetch_one(Task(url=url))
    indexed: list[str] = []
    progress = []
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
        progress.append(ok("Fetched + indexed", f"{result.url} via {result.mode}"))
        if indexed:
            progress.append(info("Tables written", ", ".join(indexed)))
        append_receipt(
            DEFAULTS.DB_PATH,
            op="browse_fetch",
            args={"url": url, "run_id": run_id},
            result=f"indexed {len(indexed)} tables: {indexed}",
        )
    else:
        progress.append(fail("Fetch failed", result.error or result.status))
    res: dict[str, Any] = {
        "ok": result.status == "ok",
        "op": "browse_fetch",
        "url": result.url,
        "mode": result.mode,
        "status": result.status,
        "elapsed_ms": result.elapsed_ms,
        "indexed": indexed,
        "error": result.error,
        "progress": progress,
    }
    if result.status != "ok":
        res["hint"] = "Use browse_locate() to probe mode first, or browse_inspect() for bot-walls."
        res["suggested_next"] = [
            next_step("browse_locate", "probe URL mode before fetching"),
            next_step("browse_inspect", "check for bot-walls or redirects"),
        ]
    else:
        preview = ""
        if isinstance(result.extracted, dict):
            preview = str(result.extracted.get("text_preview") or "")[:300]
        cite = f"{result.title or result.url} — {result.url}"
        res["cite_hints"] = [cite]
        res["suggested_next"] = [
            next_step("browse_verify", "confirm the page was indexed"),
            next_step("browse_extract", "extract specific data with CSS/XPath selector"),
            next_step("query_search", "search indexed content for keywords"),
        ]
        res["carry_forward"] = {"url": result.url, "title": result.title, "preview": preview}
        res["handover"] = {
            "instruction": (
                "The page has been fetched and indexed. "
                "Use the text_preview in extracted or query_search to find relevant passages. "
                "Cite this source at the end of your response."
            ),
            "cite_format": f"- {cite}",
        }
    res["token_estimate"] = _tok(res)
    return res


async def inspect_one(url: str) -> dict[str, Any]:
    rt = runtime()
    result = await rt.http_worker().fetch_one(Task(url=url))
    cap = get_inspect_chars()
    head = ""
    if result.status == "ok" and isinstance(result.extracted, dict):
        head = str(result.extracted.get("text_preview") or "")
        if not head:
            head = str(result.extracted)
    progress = [
        ok("Inspected", f"{result.url} — {len(head)} chars")
        if result.status == "ok"
        else fail("Inspect failed", result.error or result.status)
    ]
    res: dict[str, Any] = {
        "ok": result.status == "ok",
        "op": "browse_inspect",
        "url": result.url,
        "title": result.title,
        "status": result.status,
        "head": head[:cap],
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
        "progress": progress,
    }
    if result.status != "ok":
        res["hint"] = "Use browse_locate() to probe the URL mode, or check browse_status()."
        res["suggested_next"] = [
            next_step("browse_locate", "probe URL mode"),
            next_step("browse_status", "check engine health"),
        ]
    else:
        res["suggested_next"] = [
            next_step("browse_extract", "extract specific data with a CSS selector"),
            next_step("browse_fetch", "fetch and index this URL"),
            next_step("browse_verify", "check if already indexed"),
        ]
        res["carry_forward"] = {"url": url}
    res["token_estimate"] = _tok(res)
    return res


async def probe_one(url: str) -> dict[str, Any]:
    rt = runtime()
    domain = urlparse(url).hostname or url
    if not rt._breaker.allow(domain):
        res: dict[str, Any] = {
            "ok": False,
            "op": "browse_locate",
            "url": url,
            "domain": domain,
            "mode": "blocked",
            "error": "circuit open",
            "progress": [fail("Circuit open", domain)],
            "hint": "Circuit breaker open. Wait or call browse_status() to check.",
            "suggested_next": [next_step("browse_status", "check circuit breaker state")],
        }
        res["token_estimate"] = _tok(res)
        return res
    try:
        response = await rt.http_client().head(url, follow_redirects=True, timeout=5.0)
    except httpx.HTTPError as exc:
        res = {
            "ok": False,
            "op": "browse_locate",
            "url": url,
            "domain": domain,
            "mode": "error",
            "error": str(exc)[:80],
            "progress": [fail("HEAD probe failed", str(exc)[:60])],
            "hint": "Network error. Try browse_inspect() or check browse_status().",
            "suggested_next": [
                next_step("browse_inspect", "attempt GET with bot-wall detection"),
                next_step("browse_status", "check engine health"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    ct = response.headers.get("content-type", "").lower()
    mode = "http_json" if "json" in ct else "http_curl"
    res = {
        "ok": True,
        "op": "browse_locate",
        "url": url,
        "domain": domain,
        "mode": mode,
        "status_code": response.status_code,
        "content_type": ct,
        "progress": [ok("Probed", f"{domain} → {mode} ({response.status_code})")],
        "suggested_next": [
            next_step("browse_inspect", "peek content without indexing"),
            next_step("browse_extract", "extract structured data with CSS/XPath"),
            next_step("browse_fetch", "fetch and index"),
        ],
        "carry_forward": {"url": url, "mode": mode},
    }
    res["token_estimate"] = _tok(res)
    return res


def verify_one(url: str) -> dict[str, Any]:
    rt = runtime()
    rows = rt.query().select("SELECT * FROM pages WHERE url = ? LIMIT 1", (url,), limit=1)
    if not rows:
        res: dict[str, Any] = {
            "ok": False,
            "op": "browse_verify",
            "url": url,
            "error": "not_indexed",
            "progress": [fail("Not indexed", url)],
            "hint": "URL not in DB. Call browse_fetch() to fetch and index it first.",
            "suggested_next": [
                next_step("browse_fetch", "fetch and index this URL"),
                next_step("browse_inspect", "inspect before indexing"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    row = rows[0]
    res = {
        "ok": True,
        "op": "browse_verify",
        "url": row["url"],
        "title": row["title"],
        "domain": row["domain"],
        "status": row["status"],
        "mode": row["mode"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "progress": [ok("Verified", f"{row['url']} — {row['status']} via {row['mode']}")],
        "suggested_next": [
            next_step("browse_extract", "extract structured data with CSS/XPath"),
            next_step("query_search", "search indexed content by keyword"),
            next_step("query_select", "run custom SQL on indexed data"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


def engine_status() -> dict[str, Any]:
    s = runtime().status()
    progress = [ok("Engine healthy")] if s["ok"] else [warn("Engine issues")]
    res: dict[str, Any] = {
        **s,
        "op": "browse_status",
        "progress": progress,
        "suggested_next": [
            next_step("browse_search", "search the web"),
            next_step("query_locate", "inspect indexed data tables"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


# ── Query tier ────────────────────────────────────────────────────────────


def query_locate() -> dict[str, Any]:
    s = runtime().query().stats()
    tables = {k: v for k, v in s.items() if k != "db_bytes"}
    res: dict[str, Any] = {
        "ok": True,
        "op": "query_locate",
        "tables": tables,
        "db_bytes": s.get("db_bytes", 0),
        "progress": [ok("Listed tables", f"{len(tables)} tables")],
        "suggested_next": [
            next_step("query_search", "full-text search across indexed pages"),
            next_step("query_select", "run custom SQL"),
            next_step("query_stats", "per-table row counts"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


def query_search(query: str, table: str = "fts_pages", limit: int | None = None) -> dict[str, Any]:
    rt = runtime()
    cap = limit if limit is not None else get_max_rows()
    try:
        rows = rt.query().search(query, table=table, limit=cap + 1)
    except ValueError as exc:
        res: dict[str, Any] = {
            "ok": False,
            "op": "query_search",
            "error": str(exc),
            "hint": "table must be fts_pages|fts_news|fts_files",
            "progress": [fail("Invalid table", str(exc))],
            "suggested_next": [next_step("query_locate", "list available tables")],
        }
        res["token_estimate"] = _tok(res)
        return res
    truncated = len(rows) > cap
    rows = rows[:cap]
    progress = [ok("FTS search", f"{len(rows)} rows from {table}")]
    if truncated:
        progress.append(warn("Results capped", f"limit={cap}; narrow query or increase limit"))
    res = {
        "ok": True,
        "op": "query_search",
        "table": table,
        "query": query,
        "rows": rows,
        "total": len(rows),
        "truncated": truncated,
        "progress": progress,
        "suggested_next": [
            next_step("query_select", "refine with custom SQL"),
            next_step("browse_extract", "extract structured data from a result URL"),
            next_step("query_export", "export results to CSV/JSON"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


def query_select(sql: str, params: tuple[Any, ...] = (), limit: int | None = None) -> dict[str, Any]:
    rt = runtime()
    cap = limit if limit is not None else get_max_rows()
    try:
        rows = rt.query().select(sql, params=params, limit=cap + 1)
    except ValueError as exc:
        res: dict[str, Any] = {
            "ok": False,
            "op": "query_select",
            "error": str(exc),
            "hint": "only SELECT/WITH allowed; call query_locate() to see table names",
            "progress": [fail("SQL rejected", str(exc))],
            "suggested_next": [next_step("query_locate", "list table names")],
        }
        res["token_estimate"] = _tok(res)
        return res
    truncated = len(rows) > cap
    rows = rows[:cap]
    progress = [ok("SQL executed", f"{len(rows)} rows")]
    if truncated:
        progress.append(warn("Results capped", f"add LIMIT {cap} or use query_export() for full data"))
    res = {
        "ok": True,
        "op": "query_select",
        "rows": rows,
        "total": len(rows),
        "truncated": truncated,
        "progress": progress,
        "suggested_next": [next_step("query_export", "export full result set to CSV/JSON")],
    }
    res["token_estimate"] = _tok(res)
    return res


def query_export(table: str, out_path: str, fmt: str = "csv") -> dict[str, Any]:
    if fmt not in ("csv", "json"):
        res: dict[str, Any] = {
            "ok": False,
            "op": "query_export",
            "error": "bad_format",
            "hint": "fmt must be csv|json",
            "progress": [fail("Bad format", fmt)],
            "suggested_next": [next_step("query_locate", "list available tables")],
        }
        res["token_estimate"] = _tok(res)
        return res
    if table not in STATS_TABLES:
        res = {
            "ok": False,
            "op": "query_export",
            "error": "unknown_table",
            "hint": f"table not in {sorted(STATS_TABLES)}; call query_locate() to list tables",
            "progress": [fail("Unknown table", table)],
            "suggested_next": [next_step("query_locate", "list available tables")],
        }
        res["token_estimate"] = _tok(res)
        return res
    rt = runtime()
    try:
        if fmt == "csv":
            text = rt.query().to_csv(table)
        else:
            rows = rt.query().select(f"SELECT * FROM {table}", limit=100_000)  # noqa: S608
            text = json.dumps(rows, indent=2, default=str)
    except (ValueError, sqlite3.OperationalError) as exc:
        res = {
            "ok": False,
            "op": "query_export",
            "error": str(exc)[:80],
            "hint": "export failed; call query_stats() to verify table is non-empty",
            "progress": [fail("Export error", str(exc)[:60])],
            "suggested_next": [next_step("query_stats", "verify table is non-empty")],
        }
        res["token_estimate"] = _tok(res)
        return res
    target = resolve_path(out_path)
    snapshot(target)
    atomic_write_text(target, text)
    append_receipt(
        DEFAULTS.DB_PATH,
        op="query_export",
        args={"table": table, "out_path": out_path, "fmt": fmt},
        result=f"exported {len(text)} bytes to {target}",
    )
    res = {
        "ok": True,
        "op": "query_export",
        "path": str(target),
        "bytes": len(text),
        "progress": [ok("Exported", f"{table} → {target} ({len(text)} bytes, {fmt})")],
        "suggested_next": [next_step("query_stats", "check remaining table sizes")],
    }
    res["token_estimate"] = _tok(res)
    return res


def query_stats() -> dict[str, Any]:
    s = runtime().query().stats()
    res: dict[str, Any] = {
        "ok": True,
        "op": "query_stats",
        **s,
        "progress": [ok("Stats loaded")],
        "suggested_next": [
            next_step("query_search", "full-text search across tables"),
            next_step("query_export", "export a table to CSV/JSON"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


# ── Crawl tier ───────────────────────────────────────────────────────────────


def _crawl_worker() -> CrawlWorker:
    rt = runtime()
    return CrawlWorker(rt.http_client(), rt._breaker, rt._limiter)


async def crawl_locate(url: str) -> dict[str, Any]:
    base = await probe_one(url)
    base["op"] = "crawl_locate"
    base["base_path"] = urlparse(url).path or "/"
    base["suggested_next"] = [
        next_step("crawl_plan", "enumerate would-be frontier (dry-run)"),
        next_step("crawl_run", "start bounded crawl"),
    ]
    return base


async def crawl_plan(url: str, max_links: int = 25) -> dict[str, Any]:
    worker = _crawl_worker()
    report = await worker.run(CrawlTask(url=url, crawl_depth=1, max_pages=1))
    if not report.pages:
        res: dict[str, Any] = {
            "ok": False,
            "op": "crawl_plan",
            "error": "fetch_failed",
            "progress": [fail("Seed fetch failed", url)],
            "hint": "Use crawl_locate() to probe mode, or browse_inspect() for bot-walls.",
            "suggested_next": [
                next_step("crawl_locate", "probe crawl mode for this URL"),
                next_step("browse_inspect", "check for bot-walls"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    seed = report.pages[0]
    res = {
        "ok": seed.status == "ok",
        "op": "crawl_plan",
        "url": seed.url,
        "title": seed.title,
        "links": seed.links[:max_links],
        "files": seed.files[:max_links],
        "elapsed_ms": seed.elapsed_ms,
        "progress": [ok("Frontier enumerated", f"{len(seed.links)} links, {len(seed.files)} files")],
        "suggested_next": [next_step("crawl_run", f"crawl up to {get_max_pages()} pages")],
        "carry_forward": {"url": url},
    }
    res["token_estimate"] = _tok(res)
    return res


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
    from datetime import UTC, datetime

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    rt.db().execute(
        "INSERT OR IGNORE INTO runs (run_id, started_at) VALUES (?, ?)",
        (rid, started_at),
    )
    rt.db().commit()
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
                "url": page.url,
                "title": page.title,
                "status": page.status,
                "mode": page.mode,
                "elapsed_ms": page.elapsed_ms,
                "extracted": page.extracted,
                "group": page.group,
                "links": page.links,
                "extractedAt": page.extracted_at,
            },
            run_id=rid,
        )
    ok_pages = len(report.pages) - report.errors
    rt.db().execute(
        "UPDATE runs SET finished_at=?, completed=?, errors=? WHERE run_id=?",
        (report.finished_at, ok_pages, report.errors, rid),
    )
    rt.db().commit()
    progress = [ok("Crawl complete", f"{ok_pages}/{len(report.pages)} pages indexed")]
    if report.errors:
        progress.append(warn("Errors", f"{report.errors} pages failed"))
    if report.files_discovered:
        progress.append(info("Files found", str(report.files_discovered)))
    append_receipt(
        DEFAULTS.DB_PATH,
        op="crawl_run",
        args={"url": url, "max_pages": pages_cap, "max_depth": depth_cap, "run_id": rid},
        result=f"{ok_pages} pages indexed, {report.errors} errors",
    )
    res: dict[str, Any] = {
        "ok": report.errors < len(report.pages),
        "op": "crawl_run",
        "run_id": report.run_id,
        "seed_url": report.seed_url,
        "pages": len(report.pages),
        "errors": report.errors,
        "files_discovered": report.files_discovered,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "progress": progress,
        "suggested_next": [
            next_step("crawl_verify", f"verify run {rid}"),
            next_step("query_search", "search crawled content"),
            next_step("browse_extract", "extract structured data from a crawled URL"),
        ],
        "carry_forward": {"run_id": rid},
    }
    res["token_estimate"] = _tok(res)
    return res


async def crawl_resume(run_id: str, url: str, max_pages: int | None = None) -> dict[str, Any]:
    return await crawl_run(url, max_pages=max_pages, run_id=run_id)


def crawl_verify(run_id: str) -> dict[str, Any]:
    rt = runtime()
    run_rows = rt.query().select("SELECT run_id FROM runs WHERE run_id = ?", params=(run_id,), limit=1)
    if not run_rows:
        res: dict[str, Any] = {
            "ok": False,
            "op": "crawl_verify",
            "run_id": run_id,
            "error": "not_found",
            "progress": [fail("Run not found", run_id)],
            "hint": "Run ID not in runs table. Call crawl_run() first.",
            "suggested_next": [next_step("query_locate", "list tables and check task_log")],
        }
        res["token_estimate"] = _tok(res)
        return res
    page_rows = rt.query().select(
        "SELECT COUNT(*) AS pages FROM pages WHERE run_id = ?",
        params=(run_id,),
        limit=1,
    )
    pages = int((page_rows[0].get("pages") if page_rows else None) or 0)
    err_rows = rt.query().select(
        "SELECT COUNT(*) AS errors FROM pages WHERE run_id = ? AND status = 'error'",
        params=(run_id,),
        limit=1,
    )
    errors = int((err_rows[0].get("errors") if err_rows else None) or 0)
    progress = [ok("Run verified", f"{pages} pages, {errors} errors")]
    if errors:
        progress.append(warn("Errors present", f"{errors} failed pages"))
    res = {
        "ok": True,
        "op": "crawl_verify",
        "run_id": run_id,
        "pages": pages,
        "errors": errors,
        "progress": progress,
        "suggested_next": [
            next_step("query_search", "search crawled content"),
            next_step("browse_extract", "extract structured data from a crawled URL"),
            next_step("crawl_resume", "resume crawl if incomplete"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


# ── Extract tier ─────────────────────────────────────────────────────────────


async def extract_from_url(
    url: str,
    selector: str,
    mode: str = "css",
    output_type: str = "text",
    limit: int | None = None,
) -> dict[str, Any]:
    from engine.workers.extractor import HtmlExtractor

    rt = runtime()
    cap = limit if limit is not None else get_max_results()
    result = await rt.http_worker().fetch_one(Task(url=url))
    if result.status != "ok":
        res: dict[str, Any] = {
            "ok": False,
            "op": "browse_extract",
            "url": url,
            "error": f"fetch failed: {result.error or result.status}",
            "progress": [fail("Fetch failed", result.error or result.status)],
            "hint": "Use browse_locate() to probe mode or browse_inspect() for bot-walls.",
            "suggested_next": [
                next_step("browse_locate", "probe URL mode"),
                next_step("browse_inspect", "check for bot-walls"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    if not result.raw_html:
        res = {
            "ok": False,
            "op": "browse_extract",
            "url": url,
            "error": "response is not HTML (JSON or binary content)",
            "progress": [fail("Not HTML", "cannot apply CSS/XPath to non-HTML response")],
            "hint": "This URL returns JSON/binary. Use browse_fetch() + query_select() instead.",
            "suggested_next": [next_step("browse_fetch", "fetch and index JSON data")],
        }
        res["token_estimate"] = _tok(res)
        return res
    try:
        ex = HtmlExtractor(result.raw_html, base_url=url)
    except RuntimeError as exc:
        res = {
            "ok": False,
            "op": "browse_extract",
            "url": url,
            "error": str(exc),
            "progress": [fail("Extractor unavailable", str(exc)[:80])],
            "hint": "Install lxml: pip install 'lxml>=4.9' 'cssselect>=1.2'",
        }
        res["token_estimate"] = _tok(res)
        return res
    if mode == "css":
        extraction = ex.css(selector, output_type=output_type, limit=cap)
    elif mode == "xpath":
        extraction = ex.xpath(selector, output_type=output_type, limit=cap)
    elif mode == "text":
        extraction = ex.find_by_text(selector, output_type=output_type, limit=cap)
    elif mode == "regex":
        extraction = ex.find_by_regex(selector, output_type=output_type, limit=cap)
    elif mode == "similar":
        extraction = ex.find_similar(text=selector, output_type=output_type, limit=cap)
    else:
        res = {
            "ok": False,
            "op": "browse_extract",
            "url": url,
            "error": f"unknown mode: {mode!r}",
            "hint": "mode must be css|xpath|text|regex|similar",
            "progress": [fail("Unknown mode", mode)],
            "suggested_next": [next_step("browse_inspect", "preview page structure")],
        }
        res["token_estimate"] = _tok(res)
        return res
    if not extraction.ok:
        res = {
            "ok": False,
            "op": "browse_extract",
            "url": url,
            "selector": selector,
            "mode": mode,
            "error": extraction.error,
            "progress": [fail("Extraction failed", extraction.error or "")],
            "hint": f"Check {mode} selector syntax. Use browse_inspect() to preview the page.",
            "suggested_next": [next_step("browse_inspect", "preview page HTML structure")],
        }
        res["token_estimate"] = _tok(res)
        return res
    progress = [ok("Extracted", f"{extraction.count} matches via {mode}:{selector}")]
    if extraction.truncated:
        progress.append(warn("Truncated", f"showing {cap} of {extraction.count} matches"))
    res = {
        "ok": True,
        "op": "browse_extract",
        "url": url,
        "selector": selector,
        "mode": mode,
        "output_type": output_type,
        "matches": extraction.matches,
        "count": extraction.count,
        "truncated": extraction.truncated,
        "elapsed_ms": result.elapsed_ms,
        "progress": progress,
        "suggested_next": [
            next_step("browse_extract", "run another selector on this page"),
            next_step("query_search", "search other indexed pages"),
        ],
        "carry_forward": {"url": url},
    }
    res["token_estimate"] = _tok(res)
    return res


# ── Research tier ─────────────────────────────────────────────────────────────

_DEPTH3_FOLLOW_LINKS = 8  # max cross-domain links to follow at depth=3

_NOISE_DOMAINS: frozenset[str] = frozenset(
    {
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "linkedin.com",
        "pinterest.com",
        "tumblr.com",
        "reddit.com",
        "t.co",
    }
)


def _is_noise_url(url: str) -> bool:
    if not url.startswith("http"):
        return True
    dom = urlparse(url).hostname or ""
    base = ".".join(dom.split(".")[-2:]) if dom.count(".") >= 1 else dom
    return base in _NOISE_DOMAINS


def _url_relevance(url: str, terms: set[str]) -> float:
    """Score a URL by how many query terms appear in its path."""
    path = urlparse(url.lower()).path
    return sum(1.0 for t in terms if t in path)


def _research_queries(query: str, breadth: int) -> list[str]:
    """Generate up to breadth search angles (no LLM — keyword expansion only)."""
    if breadth <= 1:
        return [query]
    year = datetime.now(UTC).year
    angles = [
        f"{query} {year}",
        f"{query} overview guide",
        f"{query} best practices examples",
        f"how {query} works explained",
    ]
    return [query] + angles[: breadth - 1]


def _dedup_by_domain(hits: list[dict[str, Any]], max_per_domain: int = 2) -> list[dict[str, Any]]:
    """Keep at most max_per_domain hits from any single hostname."""
    counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for h in hits:
        dom = urlparse(h["url"]).hostname or ""
        if counts.get(dom, 0) < max_per_domain:
            out.append(h)
            counts[dom] = counts.get(dom, 0) + 1
    return out


async def research_topic(
    query: str,
    depth: int = 2,
    fetch_top: int = 5,
    limit: int | None = None,
    breadth: int = 1,
) -> dict[str, Any]:
    """Search + auto-fetch + index sources. Returns rich citation package.

    depth=1   search only
    depth=2   search + parallel-fetch top fetch_top URLs  (default)
    depth=3   depth=2 + refined follow-up search
    breadth=1 single query (default)
    breadth=2 multi-angle: adds year + overview variants
    breadth=3 wider: adds best-practices + how-it-works angles
    fetch_top=0  fetch ALL results
    """
    rt = runtime()
    cap = limit if limit is not None else get_max_results()
    t0 = time.monotonic()
    progress: list[dict] = []
    errors: list[str] = []

    # ── Step 1: search (one or multiple angles) ───────────────────────────────
    all_hits: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    queries = _research_queries(query, breadth)
    last_backend = "none"
    last_sw: dict[str, Any] = {}

    first_fail_sw: dict[str, Any] | None = None
    for qi, q in enumerate(queries):
        sw = await search_web(q, limit=cap)
        if sw.get("ok") and sw.get("hits"):
            new = [h for h in sw["hits"] if h["url"] not in seen_urls]
            all_hits.extend(new)
            seen_urls.update(h["url"] for h in new)
            last_backend = str(sw.get("backend", "unknown"))
            last_sw = sw
            progress.append(ok("Search", f"[q{qi + 1}] {len(new)} new hits via {last_backend}"))
        else:
            errors.append(f"angle[{qi + 1}] search failed: {sw.get('error', 'no hits')}")
            if first_fail_sw is None:
                first_fail_sw = sw

    # Fail only when every angle returned nothing.
    if not all_hits:
        fatal = first_fail_sw or {}
        no_results: dict[str, Any] = {
            "ok": False,
            "op": "browse_research",
            "query": query,
            "error": "search_failed",
            "hint": "All search backends failed. Try browse_status() to diagnose.",
            "backend_errors": fatal.get("backend_errors", []),
            "progress": [fail("Search failed", f"all {len(queries)} angle(s) returned 0 hits")],
            "suggested_next": [next_step("browse_status", "check engine health")],
        }
        no_results["token_estimate"] = _tok(no_results)
        return no_results

    # Dedup: max 2 results per domain
    deduped_hits = _dedup_by_domain(all_hits, max_per_domain=2)

    sources: list[dict[str, Any]] = [
        {
            "rank": i + 1,
            "title": h["title"],
            "url": h["url"],
            "snippet": h["snippet"],
            "fetched": False,
            "text_preview": None,
        }
        for i, h in enumerate(deduped_hits)
    ]

    # ── Phase 2: parallel fetch + index, capture outbound links ─────────────
    page_links: dict[str, list[str]] = {}  # url → outbound links (for Phase 3a)

    if depth >= 2 and sources:
        n_fetch = len(sources) if fetch_top == 0 else min(fetch_top, len(sources))
        fetch_tasks = [rt.http_worker().fetch_one(Task(url=sources[i]["url"])) for i in range(n_fetch)]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for i, result in enumerate(fetch_results):
            rank_i = sources[i]["rank"]
            url_i = sources[i]["url"]
            if isinstance(result, BaseException):
                errors.append(f"fetch[{rank_i}]: {result}")
                progress.append(warn("Fetch error", f"[{rank_i}] {str(result)[:60]}"))
            elif result.status == "ok":
                preview = ""
                if isinstance(result.extracted, dict):
                    preview = str(result.extracted.get("text_preview") or "")[:_RESEARCH_PREVIEW]
                sources[i]["fetched"] = True
                sources[i]["text_preview"] = preview
                if result.title:
                    sources[i]["title"] = result.title
                rt.indexer().index(
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
                    run_id="research",
                )
                page_links[url_i] = result.links[:50]
                progress.append(ok("Fetched+indexed", f"[{rank_i}] {url_i}"))
            else:
                errors.append(f"fetch[{rank_i}]: {result.error or result.status}")
                progress.append(warn("Fetch skipped", f"[{rank_i}] {result.error or 'error'}"))

    # ── Phase 3: refined follow-up search (depth=3) ───────────────────────────
    if depth >= 3:
        fetched_context = " ".join(
            (s.get("text_preview") or s.get("snippet") or "")[:80] for s in sources if s["fetched"]
        )[:300]
        refined_query = f"{query} {fetched_context}".strip()[:200]
        try:
            refined_sw = await search_web(refined_query, limit=cap)
            refined_hits: list[dict[str, Any]] = refined_sw.get("hits") or []
            if refined_hits:
                new_hits = [h for h in refined_hits if h["url"] not in seen_urls]
                deduped_new = _dedup_by_domain(new_hits, max_per_domain=1)
                for j, hit in enumerate(deduped_new[:cap], len(sources) + 1):
                    sources.append(
                        {
                            "rank": j,
                            "title": hit["title"],
                            "url": hit["url"],
                            "snippet": hit["snippet"],
                            "fetched": False,
                            "text_preview": None,
                        }
                    )
                    seen_urls.add(hit["url"])
                progress.append(info("Refined search", f"+{len(deduped_new)} additional sources"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"refined_search: {exc}")

    # ── Phase 3a: link following — fetch top outbound links (depth=3) ─────────
    if depth >= 3 and page_links:
        import re as _re

        query_terms = {t for t in _re.sub(r"[^\w\s]", "", query).lower().split() if len(t) >= 3}
        link_scores: dict[str, float] = {}
        for src_url, links in page_links.items():
            src_dom = urlparse(src_url).hostname or ""
            for link in links:
                if link in seen_urls or link in link_scores or _is_noise_url(link):
                    continue
                link_dom = urlparse(link).hostname or ""
                score = _url_relevance(link, query_terms)
                # Prefer external cross-domain links — they add new perspectives
                if link_dom != src_dom:
                    score += 0.5
                if score > 0:
                    link_scores[link] = score

        # Dedup by domain (1 per domain) and take top _DEPTH3_FOLLOW_LINKS
        sorted_links = sorted(link_scores, key=lambda u: link_scores[u], reverse=True)
        domain_seen: dict[str, int] = {}
        follow_urls: list[str] = []
        for u in sorted_links:
            dom = urlparse(u).hostname or ""
            if domain_seen.get(dom, 0) < 1:
                follow_urls.append(u)
                domain_seen[dom] = 1
            if len(follow_urls) >= _DEPTH3_FOLLOW_LINKS:
                break

        if follow_urls:
            follow_tasks = [rt.http_worker().fetch_one(Task(url=u)) for u in follow_urls]
            follow_results = await asyncio.gather(*follow_tasks, return_exceptions=True)

            for j, result in enumerate(follow_results):
                url_j = follow_urls[j]
                if isinstance(result, BaseException):
                    errors.append(f"follow_link[{j + 1}]: {result}")
                elif result.status == "ok":
                    preview = ""
                    if isinstance(result.extracted, dict):
                        preview = str(result.extracted.get("text_preview") or "")[:_RESEARCH_PREVIEW]
                    rank = len(sources) + 1
                    sources.append(
                        {
                            "rank": rank,
                            "title": result.title or url_j,
                            "url": result.url,
                            "snippet": preview[:200],
                            "fetched": True,
                            "text_preview": preview,
                            "via": "link_follow",
                        }
                    )
                    seen_urls.add(url_j)
                    rt.indexer().index(
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
                        run_id="research",
                    )
                    progress.append(ok("Followed+indexed", f"link → {url_j}"))

    # ── Phase 4: FTS enrichment — retrieve key passages from indexed content ──
    key_passages: list[dict[str, Any]] = []
    fetched_so_far = [s for s in sources if s["fetched"]]
    if fetched_so_far:
        try:
            fts_rows = rt.query().search(query, table="fts_pages", limit=10)
            key_passages = [
                {
                    "url": str(r.get("url", "")),
                    "title": str(r.get("title", "")),
                    "passage": str(r.get("content") or "")[:400],
                }
                for r in fts_rows
                if r.get("url")
            ]
        except Exception:  # noqa: BLE001
            pass

    # ── Build output ──────────────────────────────────────────────────────────
    fetched = [s for s in sources if s["fetched"]]
    unfetched = [s for s in sources if not s["fetched"]]
    cite_hints = [f"[{s['rank']}] {s['title']} — {s['url']}" for s in sources]
    cite_format = "## Sources\n" + "\n".join(f"- {c}" for c in cite_hints)

    res: dict[str, Any] = {
        "ok": True,
        "op": "browse_research",
        "query": query,
        "depth": depth,
        "breadth": breadth,
        "search_backend": last_backend,
        "total_sources": len(sources),
        "fetched_count": len(fetched),
        "sources": sources,
        "key_passages": key_passages,
        "cite_hints": cite_hints,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "progress": progress,
        "handover": {
            "instruction": (
                f"{len(fetched)} sources fetched and indexed. "
                f"{len(key_passages)} key passages extracted by full-text search. "
                "Read key_passages for the most relevant content, then text_preview "
                "in each fetched source for broader context. "
                "Call query_search to retrieve full text from any indexed page. "
                "End your response with a '## Sources' section using cite_format."
            ),
            "cite_format": cite_format,
        },
        "suggested_next": [
            {
                "tool": "browse_fetch",
                "url": s["url"],
                "reason": f"[{s['rank']}] fetch full content — {s['title'][:50]}",
            }
            for s in unfetched[:5]
        ]
        + [
            next_step("query_search", "search full text of all indexed research pages"),
            next_step("browse_research", "follow-up query to go deeper"),
        ],
        "unfetched_sources": [{"rank": s["rank"], "title": s["title"], "url": s["url"]} for s in unfetched],
        "carry_forward": {
            "query": query,
            "urls": [s["url"] for s in sources[:10]],
        },
    }
    if last_sw.get("current_date"):
        res["current_date"] = last_sw["current_date"]
    if errors:
        res["errors"] = errors
    res["token_estimate"] = _tok(res)
    return res
