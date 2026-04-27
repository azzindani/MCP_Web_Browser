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
