"""Web search worker. Keyless backends, tried in priority order.

Order: SearXNG (self-hosted JSON API) → DuckDuckGo HTML → Brave HTML.
Browser-rendered fallback is not implemented here; the scheduler can
upgrade a failed search to `browser_worker.search()` if it wants to.

The worker reuses the same circuit-breaker, rate-limiter, and retry
layer as the HTTP worker — search endpoints are just URLs.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Final
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

from engine.config.defaults import DEFAULTS
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.resilience.retry import with_retry
from shared.platform_utils import get_max_results

_SEARXNG_URL: Final[str] = os.environ.get(
    "MCP_SEARCH_BACKEND", "http://127.0.0.1:8888"
)
_DDG_URL: Final[str] = "https://html.duckduckgo.com/html/"
_BRAVE_URL: Final[str] = "https://search.brave.com/search"


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    backend: str


@dataclass(slots=True)
class SearchResult:
    query: str
    hits: list[SearchHit]
    backend: str  # "searxng" | "ddg" | "brave" | "none"
    elapsed_ms: int
    truncated: bool
    total: int  # before truncation
    fetched_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


# ── Body parsers (no HTML library; surgical regex on bounded chunks) ──

_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)

_BRAVE_RESULT_RE = re.compile(
    r'<div[^>]+data-type="web"[^>]*>.*?'
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>.*?'
    r'<span[^>]+class="title[^"]*"[^>]*>(.*?)</span>.*?'
    r'<div[^>]+class="snippet[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return unescape(_TAG_RE.sub("", s)).strip()


def _resolve_ddg_redirect(href: str) -> str:
    """DDG HTML wraps each result URL in /l/?uddg=…&kh=…&rut=…."""
    if href.startswith("/l/?") or href.startswith("//duckduckgo.com/l/?"):
        try:
            qs = parse_qs(urlparse(urljoin("https://duckduckgo.com/", href)).query)
            uddg = qs.get("uddg") or []
            if uddg:
                return uddg[0]
        except ValueError:
            pass
    return href


def _parse_ddg(body: str, cap: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for match in _DDG_RESULT_RE.finditer(body):
        if len(hits) >= cap:
            break
        href, raw_title, raw_snippet = match.groups()
        url = _resolve_ddg_redirect(unescape(href))
        if not url.startswith("http"):
            continue
        hits.append(
            SearchHit(
                title=_strip_tags(raw_title),
                url=url,
                snippet=_strip_tags(raw_snippet),
                backend="ddg",
            )
        )
    return hits


def _parse_brave(body: str, cap: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for match in _BRAVE_RESULT_RE.finditer(body):
        if len(hits) >= cap:
            break
        href, raw_title, raw_snippet = match.groups()
        hits.append(
            SearchHit(
                title=_strip_tags(raw_title),
                url=unescape(href),
                snippet=_strip_tags(raw_snippet),
                backend="brave",
            )
        )
    return hits
