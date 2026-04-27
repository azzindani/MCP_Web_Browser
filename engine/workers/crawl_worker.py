"""BFS crawler — static HTML pages and file discovery.

Politeness rules: stays inside the starting URL's path prefix, drops
static assets (css/js/fonts/images), respects circuit breaker and rate
limiter per domain, caps depth + total pages.

A single crawl call is bounded; longer runs are resumable through the
checkpoint module. The MCP `crawl_run` tool always passes
`max_pages` and `max_depth` via `shared.platform_utils`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from engine.config.defaults import DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from engine.core.checkpoint import Checkpoint
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter

_STATIC_EXTENSIONS: frozenset[str] = frozenset({
    ".css", ".js", ".mjs", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
})

_LINK_RE = re.compile(
    r'href=["\']([^"\'#?][^"\']*?)["\']', re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _base_path(url: str) -> str:
    """Path prefix that future BFS hops must remain under."""
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return "/"
    if "/" not in path:
        return "/"
    cut = path.rsplit("/", 1)[0] + "/"
    return cut or "/"


def _ext_of(url: str) -> str:
    try:
        return os.path.splitext(urlparse(url).path)[1].lower()
    except ValueError:
        return ""


def _content_type_to_ext(content_type: str) -> str:
    """Best-effort mime → extension fallback for URLs without a suffix."""
    ct = content_type.lower()
    if "pdf" in ct:
        return ".pdf"
    if "spreadsheetml" in ct or "excel" in ct:
        return ".xlsx"
    if "csv" in ct:
        return ".csv"
    if "zip" in ct:
        return ".zip"
    if "msword" in ct or "wordprocessingml" in ct:
        return ".docx"
    if "presentationml" in ct or "powerpoint" in ct:
        return ".pptx"
    return ""


@dataclass
class CrawlTask:
    url: str
    name: str = ""
    group: str = ""
    crawl_depth: int = 1
    max_pages: int = DEFAULTS.MAX_CRAWL_PAGES


@dataclass
class CrawlPageResult:
    task: CrawlTask
    status: str  # ok | error
    mode: str = "crawl"
    url: str = ""
    title: str = ""
    links: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None
    group: str = ""
    extracted_at: str = ""


@dataclass
class CrawlRunReport:
    run_id: str
    seed_url: str
    pages: list[CrawlPageResult]
    visited: int
    errors: int
    started_at: str
    finished_at: str

    @property
    def files_discovered(self) -> int:
        return sum(len(p.files) for p in self.pages)
