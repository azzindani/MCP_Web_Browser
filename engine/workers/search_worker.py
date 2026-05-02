"""Web search worker. Keyless backends, tried in priority order.

Order: SearXNG (self-hosted JSON API) → DuckDuckGo HTML → Brave HTML.
Browser-rendered fallback is not implemented here; the scheduler can
upgrade a failed search to `browser_worker.search()` if it wants to.

The worker reuses the same circuit-breaker, rate-limiter, and retry
layer as the HTTP worker — search endpoints are just URLs.
"""

from __future__ import annotations

import asyncio
import base64
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
_BING_URL: Final[str] = "https://www.bing.com/search"
_DDG_LITE_URL: Final[str] = "https://lite.duckduckgo.com/lite/"
_BRAVE_URL: Final[str] = "https://search.brave.com/search"

# MCP_SEARCH_LANG   — BCP-47 language tag for Bing setlang (default: en-US).
# MCP_SEARCH_MARKET — Bing market code (default: en-US for English global
#                     results regardless of IP location). Override with e.g.
#                     id-ID for Indonesian results or ja-JP for Japanese.
_SEARCH_LANG: Final[str] = os.environ.get("MCP_SEARCH_LANG", "en-US")
_SEARCH_MARKET: Final[str] = os.environ.get("MCP_SEARCH_MARKET") or "en-US"

# Fail fast — DDG/Brave consistently fail; don't burn time waiting.
_SEARCH_TIMEOUT: Final[float] = 4.0

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

# Bing S1: classic <h2><a href="url">title</a></h2>
_BING_H2_RE = re.compile(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.DOTALL)
_BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
# Bing S2: b_algo block split (newer layouts, incl. <a href><h2> reversed nesting)
_BING_ALGO_BLOCK_RE = re.compile(
    r"<li\b[^>]+\bclass=\"[^\"]*\bb_algo\b[^\"]*\"[^>]*>(.*?)</li>",
    re.DOTALL,
)
_BING_ANY_HREF_RE = re.compile(r"href=\"(https?://[^\"]+)\"")
_BING_H2_TEXT_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)

_BRAVE_RESULT_RE = re.compile(
    r'<div[^>]+data-type="web"[^>]*>.*?'
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>.*?'
    r'<span[^>]+class="title[^"]*"[^>]*>(.*?)</span>.*?'
    r'<div[^>]+class="snippet[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")

# ISP block / captive portal titles — fail fast instead of trying to parse.
_BLOCK_PAGE_TITLES = (
    "internet positif",
    "access denied",
    "blocked",
    "attention required",
    "just a moment",
    "ddos-guard",
    "security check",
    "captcha",
    "robot check",
)


def _is_block_page(body: str) -> bool:
    """Return True if the response looks like an ISP block or bot-check page."""
    head = body[:600].lower()
    return any(t in head for t in _BLOCK_PAGE_TITLES)


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


def _resolve_bing_redirect(href: str) -> str:
    """Bing wraps real URLs in /ck/a?...&u=a1{base64url}... tracking redirects."""
    if "bing.com/ck/a" not in href:
        return href
    try:
        qs = parse_qs(urlparse(href).query)
        u_param = (qs.get("u") or [""])[0]
        if u_param.startswith("a1"):
            encoded = u_param[2:]
            padding = 4 - len(encoded) % 4
            if padding != 4:
                encoded += "=" * padding
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
            if decoded.startswith("http"):
                return decoded
    except Exception:  # noqa: BLE001
        pass
    return href


def _bing_is_internal(href: str) -> bool:
    return "bing.com" in href and "bing.com/ck/a" not in href


def _parse_bing(body: str, cap: int) -> list[SearchHit]:
    hits: list[SearchHit] = []

    # S1: classic <h2><a href="url">title</a></h2>
    for m in _BING_H2_RE.finditer(body):
        if len(hits) >= cap:
            break
        href = _resolve_bing_redirect(unescape(m.group(1)))
        if not href.startswith("http") or _bing_is_internal(href):
            continue
        title = _strip_tags(m.group(2))
        if not title:
            continue
        after = body[m.end() : m.end() + 800]
        snip_m = _BING_SNIPPET_RE.search(after)
        snippet = _strip_tags(snip_m.group(1)) if snip_m else ""
        hits.append(SearchHit(title=title, url=href, snippet=snippet, backend="bing"))

    # S2: b_algo block split — handles newer Bing reversed <a href><h2> nesting
    if not hits:
        seen_urls: set[str] = set()
        for block_m in _BING_ALGO_BLOCK_RE.finditer(body):
            if len(hits) >= cap:
                break
            btext = block_m.group(1)
            href_m = _BING_ANY_HREF_RE.search(btext)
            if not href_m:
                continue
            href = _resolve_bing_redirect(unescape(href_m.group(1)))
            if not href.startswith("http") or _bing_is_internal(href) or href in seen_urls:
                continue
            seen_urls.add(href)
            title_m = _BING_H2_TEXT_RE.search(btext)
            title = _strip_tags(title_m.group(1)) if title_m else ""
            if not title or len(title) < 3:
                continue
            snip_m = _BING_SNIPPET_RE.search(btext)
            snippet = _strip_tags(snip_m.group(1)) if snip_m else ""
            hits.append(SearchHit(title=title, url=href, snippet=snippet, backend="bing"))

    # Diversity guard: ≥80 % from the same hostname → geo-bias or bot-detection page.
    if len(hits) >= 5:
        from collections import Counter

        domain_counts = Counter(urlparse(h.url).hostname or "" for h in hits)
        top_domain_count = domain_counts.most_common(1)[0][1]
        if top_domain_count / len(hits) >= 0.8:
            return []
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
            # verify=False: curl_cffi's bundled CA certs miss some search CDN certs
            resp = session.get(
                url, params=params, headers=headers, timeout=_SEARCH_TIMEOUT, allow_redirects=True, verify=False
            )
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
        for backend in ("searxng", "ddg", "bing", "brave"):
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
            elif backend == "bing":
                hits = await self._bing(query, cap)
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

    async def _bing(self, query: str, cap: int) -> list[SearchHit]:
        domain = "www.bing.com"
        if not self._breaker.allow(domain):
            raise RuntimeError(f"{domain} circuit open")
        await self._limiter.acquire(domain)
        bing_headers = {**_SEARCH_HEADERS, "Referer": "https://www.bing.com/", "Accept-Language": "en-US,en;q=0.9"}
        bing_params: dict[str, str] = {"q": query, "count": str(cap), "setlang": _SEARCH_LANG}
        if _SEARCH_MARKET:
            bing_params["mkt"] = _SEARCH_MARKET
        if _CURL_CFFI:
            status, body = await _curl_search_get(_BING_URL, bing_params, bing_headers)
        else:
            response = await with_retry(
                lambda: self._client.get(
                    _BING_URL,
                    params=bing_params,
                    headers=bing_headers,
                    timeout=_SEARCH_TIMEOUT,
                ),
                max_retries=2,
            )
            status, body = response.status_code, response.text
        if status >= 400:
            self._breaker.failure(domain)
            raise RuntimeError(f"HTTP {status}")
        if _is_block_page(body):
            self._breaker.failure(domain)
            raise RuntimeError("ISP/bot block page detected")
        self._breaker.success(domain)
        hits = _parse_bing(body, cap)
        if not hits:
            raise RuntimeError(f"no results parsed; body[0:300]={body[:300]!r}")
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
        if _is_block_page(body):
            self._breaker.failure(domain)
            raise RuntimeError("ISP/bot block page detected")
        self._breaker.success(domain)
        hits = _parse_ddg_lite(body, cap)
        if not hits:
            raise RuntimeError(f"no results parsed; body[0:300]={body[:300]!r}")
        return hits

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
        if _is_block_page(body):
            self._breaker.failure(domain)
            raise RuntimeError("ISP/bot block page detected")
        self._breaker.success(domain)
        return _parse_brave(body, cap)

    @staticmethod
    def encode_query(query: str) -> str:
        return quote_plus(query)
