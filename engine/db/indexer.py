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
