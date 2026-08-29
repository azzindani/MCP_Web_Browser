"""HTTP worker — handles `http_json` and `http_curl` modes.

`http_json`: plain httpx async with HTTP/2 — for JSON APIs.
`http_curl`: curl_cffi with impersonate="chrome120" — TLS fingerprint
  impersonation so the TLS handshake looks like a real Chrome browser.
  Falls back to httpx if curl_cffi is not installed.

Block detection (inspired by Scrapling):
  BLOCKED_STATUS_CODES  — HTTP codes that indicate anti-bot blocking
  BOT_BODY_PATTERNS     — body substrings that confirm a bot-wall page

HTML extraction (powered by lxml via HtmlExtractor):
  For HTML responses, title + visible text preview + link list are
  extracted and stored in `HttpResult.extracted` so the indexer
  has structured data without requiring a separate tool call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from curl_cffi.requests import AsyncSession as CurlSession

    _CURL_CFFI = True
except ImportError:  # pragma: no cover
    CurlSession: Any = None  # type: ignore[misc]
    _CURL_CFFI = False

from engine.config.defaults import BOT_BODY_PATTERNS, DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.resilience.retry import with_retry

_CURL_IMPERSONATE = "chrome120"

# HTTP status codes that signal anti-bot blocking rather than permanent errors.
# These trigger `status="blocked"` so the caller can retry via browser mode.
BLOCKED_STATUS_CODES: frozenset[int] = frozenset({401, 403, 407, 429, 503})

_BASE_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULTS.USER_AGENT,
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
}

# Chrome-120 fingerprint headers for HTML page fetching.
_CURL_HEADERS: dict[str, str] = {
    **_BASE_HEADERS,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",  # stealthy: appear to come from search
    "sec-ch-ua": ('"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "cross-site",
    "sec-fetch-user": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Task:
    url: str
    name: str = ""
    mode: str = "auto"
    group: str = ""
    max_retries: int = DEFAULTS.MAX_RETRIES


@dataclass
class HttpResult:
    task: Task
    status: str  # "ok" | "error" | "blocked" | "skipped"
    mode: str  # "http_json" | "http_curl"
    url: str
    title: str = ""
    extracted: dict[str, Any] = field(default_factory=dict)
    raw_html: str = ""  # populated for HTML responses; used by browse_extract
    links: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    group: str = ""
    ticker: str | None = None
    extracted_at: str = ""


def _parse_yahoo_chart(body: Any) -> dict[str, Any]:
    """Normalise Yahoo `chart` payload into the flat shape the indexer expects."""
    try:
        chart = body["chart"]
        result_list = chart.get("result") or []
        if not result_list:
            return {}
        meta = (result_list[0] or {}).get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        change = meta.get("regularMarketChange")
        if change is None and price is not None and prev_close is not None:
            change = round(price - prev_close, 4)
        change_pct = meta.get("regularMarketChangePercent")
        if change_pct is None and price is not None and prev_close not in (None, 0):
            change_pct = round((price - prev_close) / prev_close * 100, 4)
        return {
            "price": price,
            "change": change,
            "changePct": change_pct,
            "prevClose": prev_close,
            "currency": meta.get("currency") or "IDR",
            "exchange": meta.get("exchangeName") or "",
            "company": meta.get("longName") or "",
        }
    except KeyError, TypeError, IndexError, AttributeError:
        return {}


def _is_botwall(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in BOT_BODY_PATTERNS)


def _parse_html(body: str, url: str) -> tuple[dict[str, Any], str, list[str]]:
    """Extract title, visible text, and links from an HTML body using lxml."""
    try:
        from engine.workers.extractor import HtmlExtractor

        ex = HtmlExtractor(body, base_url=url)
        title = ex.get_title()
        text = ex.get_all_text()
        links = ex.get_links()
        return (
            {
                "text_preview": text[:2_000],
                "text_length": len(text),
                "links_count": len(links),
            },
            title,
            links[:100],
        )
    except Exception:
        return {}, "", []


class HttpWorker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        breaker: CircuitBreaker,
        limiter: RateLimiter,
    ) -> None:
        self._client = client
        self._breaker = breaker
        self._limiter = limiter

    @classmethod
    def make_client(cls, timeout: float = DEFAULTS.HTTP_TIMEOUT) -> httpx.AsyncClient:
        """Construct a default httpx client."""
        return httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=timeout,
            headers=_BASE_HEADERS,
        )

    async def _stealth_get(self, url: str, domain: str, timeout: float) -> tuple[int, str, str]:  # noqa: ASYNC109
        """Fetch with curl_cffi Chrome TLS impersonation; returns (status, text, content_type)."""
        async with CurlSession(impersonate=_CURL_IMPERSONATE) as session:
            resp = await session.get(
                url,
                headers={**_CURL_HEADERS, "Referer": f"https://{domain}/"},
                timeout=timeout,
                allow_redirects=True,
            )
            ct = resp.headers.get("content-type", "")
            return resp.status_code, resp.text, ct

    async def fetch_one(self, task: Task) -> HttpResult:
        domain = _domain_of(task.url)
        mode = "http_curl" if task.mode == "http_curl" else "http_json"
        now = _now_iso()
        t0 = time.monotonic()

        if not self._breaker.allow(domain):
            return HttpResult(
                task=task,
                status="skipped",
                mode=mode,
                url=task.url,
                error="circuit open",
                group=task.group,
                extracted_at=now,
            )

        await self._limiter.acquire(domain)

        timeout = DOMAIN_CONFIG.get(domain, {}).get("timeout", DEFAULTS.HTTP_TIMEOUT)

        # http_curl mode: prefer curl_cffi TLS impersonation (Scrapling-style)
        if mode == "http_curl" and _CURL_CFFI:
            try:
                status_code, text, ct = await with_retry(
                    lambda: self._stealth_get(task.url, domain, timeout),
                    max_retries=task.max_retries,
                )
            except Exception as exc:
                self._breaker.failure(domain)
                return HttpResult(
                    task=task,
                    status="error",
                    mode=mode,
                    url=task.url,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc)[:100],
                    group=task.group,
                    extracted_at=now,
                )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if status_code in BLOCKED_STATUS_CODES:
                self._breaker.failure(domain)
                return HttpResult(
                    task=task,
                    status="blocked",
                    mode=mode,
                    url=task.url,
                    elapsed_ms=elapsed_ms,
                    error=f"HTTP {status_code} (blocked)",
                    group=task.group,
                    extracted_at=now,
                )
            if status_code >= 400:
                self._breaker.failure(domain)
                return HttpResult(
                    task=task,
                    status="error",
                    mode=mode,
                    url=task.url,
                    elapsed_ms=elapsed_ms,
                    error=f"HTTP {status_code}",
                    group=task.group,
                    extracted_at=now,
                )
            if _is_botwall(text):
                self._breaker.failure(domain)
                return HttpResult(
                    task=task,
                    status="blocked",
                    mode=mode,
                    url=task.url,
                    elapsed_ms=elapsed_ms,
                    error="bot-wall detected in response body",
                    group=task.group,
                    extracted_at=now,
                )
            self._breaker.success(domain)
            body: Any = text
            if "json" in ct or text.lstrip().startswith(("{{", "[")):
                try:
                    import json as _json

                    body = _json.loads(text)
                except ValueError:
                    pass
            extracted, title, links = self._classify_payload(body, text, task)
            return HttpResult(
                task=task,
                status="ok",
                mode=mode,
                url=task.url,
                title=title,
                extracted=extracted,
                raw_html=text if isinstance(body, str) else "",
                links=links,
                elapsed_ms=elapsed_ms,
                group=task.group,
                ticker=task.name if task.group == "Yahoo" else None,
                extracted_at=now,
            )

        # Fallback: plain httpx (http_json or curl_cffi unavailable)
        headers = _CURL_HEADERS if mode == "http_curl" else _BASE_HEADERS
        try:
            response = await with_retry(
                lambda: self._client.get(
                    task.url,
                    headers={**headers, "Referer": f"https://{domain}/"},
                    timeout=timeout,
                ),
                max_retries=task.max_retries,
            )
        except httpx.HTTPError as exc:
            self._breaker.failure(domain)
            return HttpResult(
                task=task,
                status="error",
                mode=mode,
                url=task.url,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc)[:100],
                group=task.group,
                extracted_at=now,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_code = response.status_code

        # Blocked (anti-bot): retry via browser worker is appropriate
        if status_code in BLOCKED_STATUS_CODES:
            self._breaker.failure(domain)
            return HttpResult(
                task=task,
                status="blocked",
                mode=mode,
                url=task.url,
                elapsed_ms=elapsed_ms,
                error=f"HTTP {status_code} (blocked)",
                group=task.group,
                extracted_at=now,
            )

        # Other 4xx / 5xx = hard error
        if status_code >= 400:
            self._breaker.failure(domain)
            return HttpResult(
                task=task,
                status="error",
                mode=mode,
                url=task.url,
                elapsed_ms=elapsed_ms,
                error=f"HTTP {status_code}",
                group=task.group,
                extracted_at=now,
            )

        body, text = self._parse_body(response)

        if isinstance(text, str) and _is_botwall(text):
            self._breaker.failure(domain)
            return HttpResult(
                task=task,
                status="blocked",
                mode=mode,
                url=task.url,
                elapsed_ms=elapsed_ms,
                error="bot-wall detected in response body",
                group=task.group,
                extracted_at=now,
            )

        self._breaker.success(domain)

        extracted, title, links = self._classify_payload(body, text, task)
        raw_html = text if isinstance(body, str) else ""

        return HttpResult(
            task=task,
            status="ok",
            mode=mode,
            url=task.url,
            title=title,
            extracted=extracted,
            raw_html=raw_html,
            links=links,
            elapsed_ms=elapsed_ms,
            group=task.group,
            ticker=task.name if task.group == "Yahoo" else None,
            extracted_at=now,
        )

    @staticmethod
    def _parse_body(response: httpx.Response) -> tuple[Any, str]:
        text = response.text
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or text.lstrip().startswith(("{", "[")):
            try:
                return response.json(), text
            except ValueError:
                pass
        return text, text

    @staticmethod
    def _classify_payload(body: Any, text: str, task: Task) -> tuple[dict[str, Any], str, list[str]]:
        # Yahoo Finance JSON
        if isinstance(body, dict) and "chart" in body:
            extracted = _parse_yahoo_chart(body)
            title = str(extracted.get("company") or task.name or "")
            return extracted, title, []
        # Other JSON
        if isinstance(body, dict):
            return body, task.name, []
        # HTML body — extract title + visible text + links via lxml
        if isinstance(body, str) and body.strip():
            extracted, title, links = _parse_html(body, task.url)
            return extracted, title or task.name, links
        return {}, task.name, []
