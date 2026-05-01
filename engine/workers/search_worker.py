"""Web search worker. Keyless backends, tried in priority order.

Order: SearXNG (self-hosted JSON API) → DuckDuckGo HTML → Brave HTML.
Browser-rendered fallback is not implemented here; the scheduler can
upgrade a failed search to `browser_worker.search()` if it wants to.

The worker reuses the same circuit-breaker, rate-limiter, and retry
layer as the HTTP worker — search endpoints are just URLs.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from html import unescape
from typing import Any, Final
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

try:
    from curl_cffi.requests import Session as CurlSyncSession

    _CURL_CFFI = True
except ImportError:  # pragma: no cover
    CurlSyncSession: Any = None  # type: ignore[misc]
    _CURL_CFFI = False

from engine.config.defaults import DEFAULTS
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.resilience.retry import with_retry
from shared.platform_utils import get_max_results

_CURL_IMPERSONATE = "chrome120"

_SEARXNG_URL: Final[str] = os.environ.get("MCP_SEARCH_BACKEND", "http://127.0.0.1:8888")
_DDG_LITE_URL: Final[str] = "https://lite.duckduckgo.com/lite/"
_BRAVE_URL: Final[str] = "https://search.brave.com/search"

# Shorter timeout for search — we fail fast and try the next backend.
_SEARCH_TIMEOUT: Final[float] = 8.0

# Browser-like headers that help DDG / Brave serve results instead of CAPTCHAs.
_SEARCH_HEADERS: Final[dict[str, str]] = {
    "User-Agent": DEFAULTS.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


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
    errors: list[str] = dataclass_field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


# ── Body parsers (no HTML library; surgical regex on bounded chunks) ──

# DDG Lite: result links and snippets appear in paired <td> rows.
_DDG_LITE_LINK_RE = re.compile(
    r'<td[^>]+class="result-link"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_LITE_SNIPPET_RE = re.compile(
    r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>',
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


def _parse_ddg_lite(body: str, cap: int) -> list[SearchHit]:
    links = _DDG_LITE_LINK_RE.findall(body)
    snippets = [m.group(1) for m in _DDG_LITE_SNIPPET_RE.finditer(body)]
    hits: list[SearchHit] = []
    for (href, raw_title), raw_snippet in zip(links, snippets):
        if len(hits) >= cap:
            break
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


async def _curl_search_get(url: str, params: dict[str, str], headers: dict[str, str]) -> tuple[int, str]:
    """GET via sync curl_cffi in a thread — avoids Proactor event loop issues on Windows."""

    def _sync() -> tuple[int, str]:
        with CurlSyncSession(impersonate=_CURL_IMPERSONATE) as session:
            resp = session.get(url, params=params, headers=headers, timeout=_SEARCH_TIMEOUT, allow_redirects=True)
            return resp.status_code, resp.text

    return await asyncio.to_thread(_sync)


class SearchWorker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        breaker: CircuitBreaker,
        limiter: RateLimiter,
        searxng_url: str = _SEARXNG_URL,
    ) -> None:
        self._client = client
        self._breaker = breaker
        self._limiter = limiter
        self._searxng_url = searxng_url.rstrip("/")

    async def search(self, query: str, limit: int | None = None) -> SearchResult:
        cap = limit if limit is not None else get_max_results()
        t0 = time.monotonic()
        errors: list[str] = []
        for backend in ("searxng", "ddg", "brave"):
            hits, err = await self._try_backend(backend, query, cap)
            if err:
                errors.append(f"{backend}: {err}")
            if hits:
                return SearchResult(
                    query=query,
                    hits=hits[:cap],
                    backend=backend,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    truncated=len(hits) > cap,
                    total=len(hits),
                    fetched_at=_now_iso(),
                    errors=errors,
                )
        return SearchResult(
            query=query,
            hits=[],
            backend="none",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            truncated=False,
            total=0,
            fetched_at=_now_iso(),
            errors=errors,
        )

    async def _try_backend(self, backend: str, query: str, cap: int) -> tuple[list[SearchHit], str]:
        try:
            if backend == "searxng":
                hits = await self._searxng(query, cap)
            elif backend == "ddg":
                hits = await self._ddg(query, cap)
            elif backend == "brave":
                hits = await self._brave(query, cap)
            else:
                hits = []
            return hits, "" if hits else "no results parsed"
        except Exception as exc:  # noqa: BLE001
            return [], f"{type(exc).__name__}: {exc}"

    async def _searxng(self, query: str, cap: int) -> list[SearchHit]:
        domain = _domain_of(self._searxng_url) or "searxng"
        if not self._breaker.allow(domain):
            raise RuntimeError(f"{domain} circuit open")
        await self._limiter.acquire(domain)
        url = f"{self._searxng_url}/search"
        response = await with_retry(
            lambda: self._client.get(
                url,
                params={"q": query, "format": "json", "safesearch": "0"},
                timeout=_SEARCH_TIMEOUT,
            ),
            max_retries=2,
        )
        if response.status_code >= 400:
            self._breaker.failure(domain)
            raise RuntimeError(f"HTTP {response.status_code}")
        data = response.json()
        self._breaker.success(domain)
        hits: list[SearchHit] = []
        for item in data.get("results", [])[:cap]:
            url_ = item.get("url")
            if not isinstance(url_, str):
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or ""),
                    url=url_,
                    snippet=str(item.get("content") or ""),
                    backend="searxng",
                )
            )
        return hits

    async def _ddg(self, query: str, cap: int) -> list[SearchHit]:
        domain = "lite.duckduckgo.com"
        if not self._breaker.allow(domain):
            raise RuntimeError(f"{domain} circuit open")
        await self._limiter.acquire(domain)
        if _CURL_CFFI:
            status, body = await _curl_search_get(_DDG_LITE_URL, {"q": query}, _SEARCH_HEADERS)
        else:
            response = await with_retry(
                lambda: self._client.get(
                    _DDG_LITE_URL,
                    params={"q": query},
                    headers=_SEARCH_HEADERS,
                    timeout=_SEARCH_TIMEOUT,
                ),
                max_retries=2,
            )
            status, body = response.status_code, response.text
        if status >= 400:
            self._breaker.failure(domain)
            raise RuntimeError(f"HTTP {status}")
        self._breaker.success(domain)
        return _parse_ddg_lite(body, cap)

    async def _brave(self, query: str, cap: int) -> list[SearchHit]:
        domain = "search.brave.com"
        if not self._breaker.allow(domain):
            raise RuntimeError(f"{domain} circuit open")
        await self._limiter.acquire(domain)
        brave_headers = {**_SEARCH_HEADERS, "Referer": "https://www.google.com/"}
        if _CURL_CFFI:
            status, body = await _curl_search_get(_BRAVE_URL, {"q": query, "source": "web"}, brave_headers)
        else:
            response = await with_retry(
                lambda: self._client.get(
                    _BRAVE_URL,
                    params={"q": query, "source": "web"},
                    headers=brave_headers,
                    timeout=_SEARCH_TIMEOUT,
                ),
                max_retries=2,
            )
            status, body = response.status_code, response.text
        if status >= 400:
            self._breaker.failure(domain)
            raise RuntimeError(f"HTTP {status}")
        self._breaker.success(domain)
        return _parse_brave(body, cap)

    @staticmethod
    def encode_query(query: str) -> str:
        return quote_plus(query)
