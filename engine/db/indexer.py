"""Atomic multi-table indexer.

`Indexer.index(result, run_id)` routes a single engine result into all
relevant tables (pages, stocks, news, market_indices, files, links,
endpoints, domains, plus their FTS5 mirrors) inside one transaction.
SHA-256 dedup keeps news and files unique by content / URL hash.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

_FTS_PAGE_CONTENT_CAP = 50_000


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return url
    return host or url


_NUMBER_SUFFIX_MULTIPLIERS = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}


def safe_float(v: Any) -> float | None:
    """Parse a Yahoo/Investing-style number ('1.2M', '3,400', '5.6%')."""
    if v is None or v == "":
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s:
        return None
    suffix = s[-1].upper()
    if suffix in _NUMBER_SUFFIX_MULTIPLIERS:
        try:
            return float(s[:-1]) * _NUMBER_SUFFIX_MULTIPLIERS[suffix]
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class IndexReport:
    indexed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_SQL_UPSERT_PAGE = """
INSERT INTO pages
    (url, domain, title, status, mode, content_hash,
     first_seen, last_seen, elapsed_ms, source_group, run_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET
    last_seen    = excluded.last_seen,
    content_hash = excluded.content_hash,
    elapsed_ms   = excluded.elapsed_ms,
    title        = COALESCE(excluded.title, title)
"""

_SQL_DELETE_FTS_PAGE = "DELETE FROM fts_pages WHERE url = ?"
_SQL_INSERT_FTS_PAGE = (
    "INSERT INTO fts_pages (url, title, content, source) VALUES (?, ?, ?, ?)"
)

_SQL_INSERT_STOCK = """
INSERT INTO stocks
    (ticker, company_name, price, change_val, change_pct,
     volume, market_cap, day_high, day_low, prev_close,
     week52_high, week52_low, currency, exchange,
     source_url, extracted_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SQL_INSERT_NEWS = """
INSERT OR IGNORE INTO news
    (source, headline, url, content, extracted_at, content_hash)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SQL_INSERT_FTS_NEWS = (
    "INSERT INTO fts_news (headline, source, url, content) VALUES (?, ?, ?, ?)"
)

_SQL_INSERT_INDEX = """
INSERT INTO market_indices
    (index_name, price, change_val, change_pct, source_url, extracted_at)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SQL_INSERT_FILE = """
INSERT OR IGNORE INTO files
    (source_url, discovered_from, filename, ext,
     content_hash, extracted_at, status)
VALUES (?, ?, ?, ?, ?, ?, 'discovered')
"""

_SQL_INSERT_FTS_FILE = (
    "INSERT INTO fts_files (filename, source_url, ext, content_text) "
    "VALUES (?, ?, ?, ?)"
)

_SQL_INSERT_LINK = """
INSERT INTO links (from_url, to_url, anchor_text, discovered_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(from_url, to_url) DO NOTHING
"""

_SQL_INSERT_ENDPOINT = """
INSERT INTO endpoints
    (url, method, discovered_from, params,
     response_schema, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET last_seen = excluded.last_seen
"""

_SQL_UPSERT_DOMAIN = """
INSERT INTO domains
    (domain, mode, last_seen, total_pages, total_errors,
     avg_ms, circuit_state, updated_at)
VALUES (?, ?, ?, 1, ?, ?, 'closed', ?)
ON CONFLICT(domain) DO UPDATE SET
    mode         = COALESCE(excluded.mode, mode),
    last_seen    = excluded.last_seen,
    total_pages  = total_pages + 1,
    total_errors = total_errors + excluded.total_errors,
    avg_ms       = (COALESCE(avg_ms, 0) * total_pages + excluded.avg_ms)
                   / (total_pages + 1),
    updated_at   = excluded.updated_at
"""

_SQL_LOG_TASK = """
INSERT INTO task_log
    (run_id, task_id, task_name, url, mode, status,
     elapsed_ms, error, ts)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class Indexer:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── public entry points ─────────────────────────────────────────

    def index(self, result: dict[str, Any], run_id: str) -> IndexReport:
        report = IndexReport()
        try:
            with self._conn:  # atomic transaction
                self._index_page(result, run_id, report)
                self._route_extracted(result, report)
                self._index_links(result, report)
                self._update_domain(result)
        except sqlite3.Error as exc:
            report.errors.append(str(exc)[:100])
        return report

    def log_task(
        self,
        run_id: str,
        task_id: str,
        task_name: str,
        url: str,
        mode: str,
        status: str,
        elapsed_ms: int,
        error: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                _SQL_LOG_TASK,
                (run_id, task_id, task_name, url, mode, status,
                 elapsed_ms, error, _now_iso()),
            )

    # ── private routers ─────────────────────────────────────────────

    def _index_page(
        self, result: dict[str, Any], run_id: str, report: IndexReport
    ) -> None:
        url = result.get("url")
        if not isinstance(url, str) or not url:
            return

        domain = _domain_of(url)
        now = _now_iso()
        content = self._extract_content(result)
        elapsed = result.get("elapsed_ms") or result.get("elapsedMs")
        title = result.get("title") or ""
        source_group = result.get("group") or result.get("source") or ""

        self._conn.execute(
            _SQL_UPSERT_PAGE,
            (
                url, domain, title,
                result.get("status") or "",
                result.get("mode") or "",
                _sha256(content) if content else None,
                now, now,
                int(elapsed) if isinstance(elapsed, (int, float)) else None,
                source_group, run_id,
            ),
        )
        # Refresh FTS row.
        self._conn.execute(_SQL_DELETE_FTS_PAGE, (url,))
        self._conn.execute(
            _SQL_INSERT_FTS_PAGE,
            (url, title, content[:_FTS_PAGE_CONTENT_CAP], source_group),
        )
        report.indexed.append("page")

    def _route_extracted(
        self, result: dict[str, Any], report: IndexReport
    ) -> None:
        extracted = result.get("extracted") or {}
        if not isinstance(extracted, dict):
            return
        self._route_stock(result, extracted, report)
        self._route_news(result, extracted, report)
        self._route_index(result, extracted, report)
        self._route_files(result, extracted, report)
        self._route_endpoints(result, extracted, report)

    def _route_stock(
        self,
        result: dict[str, Any],
        extracted: dict[str, Any],
        report: IndexReport,
    ) -> None:
        group = result.get("group") or ""
        source = result.get("source") or ""
        extract_type = result.get("extractType") or ""
        looks_like_stock = (
            group == "Yahoo"
            or source == "yahoo_http"
            or extract_type == "stock_price"
            or extracted.get("price") is not None
        )
        if not looks_like_stock:
            return
        ticker = result.get("ticker") or (
            (result.get("name") or "").split(" Stock")[0]
        )
        if not ticker:
            return

        url = result.get("url") or ""
        now = _now_iso()
        self._conn.execute(
            _SQL_INSERT_STOCK,
            (
                ticker,
                extracted.get("companyName")
                or extracted.get("company_name")
                or result.get("company") or "",
                safe_float(extracted.get("price") or result.get("price")),
                safe_float(extracted.get("change") or extracted.get("change_val")),
                safe_float(extracted.get("changePct") or extracted.get("change_pct")),
                safe_float(extracted.get("volume")),
                safe_float(extracted.get("marketCap") or extracted.get("market_cap")),
                safe_float(extracted.get("dayHigh") or extracted.get("day_high")),
                safe_float(extracted.get("dayLow") or extracted.get("day_low")),
                safe_float(extracted.get("prevClose") or result.get("prev_close")),
                safe_float(extracted.get("week52High")),
                safe_float(extracted.get("week52Low")),
                result.get("currency") or "IDR",
                result.get("exchange") or "",
                url,
                result.get("extractedAt") or now,
            ),
        )
        report.indexed.append("stock")

    def _route_news(
        self,
        result: dict[str, Any],
        extracted: dict[str, Any],
        report: IndexReport,
    ) -> None:
        headlines = extracted.get("headlines")
        if not isinstance(headlines, list) or not headlines:
            return
        url = result.get("url") or ""
        group = result.get("group") or result.get("source") or "unknown"
        now = result.get("extractedAt") or _now_iso()
        count = 0
        for h in headlines:
            if not isinstance(h, str) or len(h) < 5:
                continue
            cur = self._conn.execute(
                _SQL_INSERT_NEWS,
                (group, h, url, "", now, _sha256(h)),
            )
            if cur.rowcount:
                self._conn.execute(_SQL_INSERT_FTS_NEWS, (h, group, url, ""))
                count += 1
        if count:
            report.indexed.append(f"news:{count}")

    def _route_index(
        self,
        result: dict[str, Any],
        extracted: dict[str, Any],
        report: IndexReport,
    ) -> None:
        if (
            result.get("group") != "INVESTING"
            and result.get("extractType") != "index_price"
        ):
            return
        if extracted.get("price") is None:
            return
        url = result.get("url") or ""
        now = result.get("extractedAt") or _now_iso()
        self._conn.execute(
            _SQL_INSERT_INDEX,
            (
                result.get("name") or "",
                safe_float(extracted.get("price")),
                extracted.get("change"),
                extracted.get("changePct"),
                url,
                now,
            ),
        )
        report.indexed.append("index")

    def _route_files(
        self,
        result: dict[str, Any],
        extracted: dict[str, Any],
        report: IndexReport,
    ) -> None:
        files = extracted.get("files")
        if not isinstance(files, list) or not files:
            return
        url = result.get("url") or ""
        now = _now_iso()
        count = 0
        for f in files:
            if not isinstance(f, dict):
                continue
            f_url = f.get("url")
            if not isinstance(f_url, str) or not f_url:
                continue
            try:
                path = urlparse(f_url).path
            except ValueError:
                continue
            filename = os.path.basename(path) or f_url
            ext = (f.get("ext") or os.path.splitext(filename)[1] or "").lower()
            cur = self._conn.execute(
                _SQL_INSERT_FILE,
                (f_url, url, filename, ext, _sha256(f_url), now),
            )
            if cur.rowcount:
                self._conn.execute(
                    _SQL_INSERT_FTS_FILE, (filename, f_url, ext, "")
                )
                count += 1
        if count:
            report.indexed.append(f"files:{count}")

    def _route_endpoints(
        self,
        result: dict[str, Any],
        extracted: dict[str, Any],
        report: IndexReport,
    ) -> None:
        import json

        endpoints = extracted.get("endpoints")
        if not isinstance(endpoints, list) or not endpoints:
            return
        url = result.get("url") or ""
        now = _now_iso()
        count = 0
        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            ep_url = ep.get("url")
            if not isinstance(ep_url, str) or not ep_url:
                continue
            self._conn.execute(
                _SQL_INSERT_ENDPOINT,
                (
                    ep_url,
                    ep.get("method") or "GET",
                    url,
                    None,
                    json.dumps(ep.get("response_keys") or []),
                    now, now,
                ),
            )
            count += 1
        if count:
            report.indexed.append(f"endpoints:{count}")

    def _index_links(
        self, result: dict[str, Any], report: IndexReport
    ) -> None:
        links = result.get("links")
        if not isinstance(links, list) or not links:
            return
        from_url = result.get("url") or ""
        now = _now_iso()
        count = 0
        for link in links:
            if not isinstance(link, str):
                continue
            self._conn.execute(_SQL_INSERT_LINK, (from_url, link, None, now))
            count += 1
        if count:
            report.indexed.append(f"links:{count}")

    def _update_domain(self, result: dict[str, Any]) -> None:
        url = result.get("url")
        if not isinstance(url, str) or not url:
            return
        domain = _domain_of(url)
        now = _now_iso()
        is_error = result.get("status") in {"error", "blocked", "timeout"}
        elapsed = result.get("elapsed_ms") or result.get("elapsedMs") or 0
        self._conn.execute(
            _SQL_UPSERT_DOMAIN,
            (
                domain,
                result.get("mode"),
                now,
                1 if is_error else 0,
                int(elapsed) if isinstance(elapsed, (int, float)) else 0,
                now,
            ),
        )

    @staticmethod
    def _extract_content(result: dict[str, Any]) -> str:
        ext = result.get("extracted") or {}
        if not isinstance(ext, dict):
            return ""
        for key in ("text_preview", "preview"):
            v = ext.get(key)
            if isinstance(v, str):
                return v[:_FTS_PAGE_CONTENT_CAP]
        headlines = ext.get("headlines")
        if isinstance(headlines, list):
            joined = "\n".join(h for h in headlines if isinstance(h, str))
            return joined[:_FTS_PAGE_CONTENT_CAP]
        return ""
