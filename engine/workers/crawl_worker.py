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
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from engine.config.defaults import DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from engine.core.checkpoint import Checkpoint
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter

_STATIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
    }
)

_LINK_RE = re.compile(r'href=["\']([^"\'#?][^"\']*?)["\']', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


class CrawlWorker:
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
    def make_client(cls, timeout: float = DEFAULTS.CRAWL_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": DEFAULTS.USER_AGENT,
                "Accept": "text/html,*/*",
                "Accept-Language": DEFAULTS.LOCALE + ",en;q=0.7",
            },
        )

    async def run(self, task: CrawlTask, *, checkpoint: Checkpoint | None = None) -> CrawlRunReport:
        started = _now_iso()
        run_id = checkpoint.run_id() if checkpoint else f"crawl-{int(time.time())}"
        seed_domain = _domain_of(task.url)
        base = _base_path(task.url)

        visited: set[str] = set(checkpoint.done_urls()) if checkpoint else set()
        visited.add(task.url)
        # (url, depth)
        queue: list[tuple[str, int]] = [(task.url, 0)]
        results: list[CrawlPageResult] = []
        errors = 0

        while queue and len(results) < task.max_pages:
            url, depth = queue.pop(0)
            if checkpoint and url in checkpoint.done_urls() and url != task.url:
                continue

            domain = _domain_of(url)
            if not self._breaker.allow(domain):
                continue
            await self._limiter.acquire(domain)

            page = await self._fetch_page(task, url, depth, seed_domain)
            results.append(page)
            if page.status == "error":
                errors += 1

            visited.add(url)
            if checkpoint is not None:
                checkpoint.save(visited, total_count=len(queue) + len(results))

            if page.status == "ok" and depth < task.crawl_depth:
                for link in page.links:
                    if link in visited:
                        continue
                    try:
                        if not urlparse(link).path.startswith(base):
                            continue
                    except ValueError:
                        continue
                    visited.add(link)
                    queue.append((link, depth + 1))

        return CrawlRunReport(
            run_id=run_id,
            seed_url=task.url,
            pages=results,
            visited=len(visited),
            errors=errors,
            started_at=started,
            finished_at=_now_iso(),
        )

    async def _fetch_page(
        self,
        task: CrawlTask,
        url: str,
        depth: int,
        seed_domain: str,
    ) -> CrawlPageResult:
        domain = _domain_of(url)
        timeout = DOMAIN_CONFIG.get(domain, {}).get("timeout", DEFAULTS.CRAWL_TIMEOUT)
        t0 = time.monotonic()
        now = _now_iso()
        try:
            response = await self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            self._breaker.failure(domain)
            return CrawlPageResult(
                task=task,
                status="error",
                url=url,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc)[:100],
                group=task.group,
                extracted_at=now,
            )

        if response.status_code >= 400:
            self._breaker.failure(domain)
            return CrawlPageResult(
                task=task,
                status="error",
                url=url,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=f"HTTP {response.status_code}",
                group=task.group,
                extracted_at=now,
            )

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            return self._handle_non_html(task, url, response, t0, now)

        html = response.text
        title_match = _TITLE_RE.search(html)
        title = title_match.group(1).strip() if title_match else ""

        try:
            base_url = response.url
        except AttributeError:
            base_url = url

        links: list[str] = []
        files: list[dict[str, str]] = []
        seen_files: dict[str, dict[str, str]] = {}

        for match in _LINK_RE.finditer(html):
            href = match.group(1).strip()
            try:
                full = urljoin(str(base_url), href)
            except ValueError:
                continue
            ext = _ext_of(full)
            if ext in DEFAULTS.COLLECTIBLE_EXTENSIONS:
                if full not in seen_files:
                    seen_files[full] = {"url": full, "text": href, "ext": ext}
                continue
            if ext in _STATIC_EXTENSIONS:
                continue
            if not full.startswith("http"):
                continue
            if _domain_of(full) != seed_domain:
                continue
            links.append(full)

        unique_links = list(dict.fromkeys(links))[:50]
        files = list(seen_files.values())
        self._breaker.success(domain)

        return CrawlPageResult(
            task=task,
            status="ok",
            url=url,
            title=title,
            links=unique_links,
            files=files,
            extracted={"files": files, "links": unique_links, "depth": depth},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            group=task.group,
            extracted_at=now,
        )

    def _handle_non_html(
        self,
        task: CrawlTask,
        url: str,
        response: httpx.Response,
        t0: float,
        now: str,
    ) -> CrawlPageResult:
        domain = _domain_of(url)
        try:
            filename = os.path.basename(urlparse(url).path) or url
        except ValueError:
            filename = url
        ext = _ext_of(url)
        if ext not in DEFAULTS.COLLECTIBLE_EXTENSIONS:
            ext = _content_type_to_ext(response.headers.get("content-type", ""))

        self._breaker.success(domain)
        if ext in DEFAULTS.COLLECTIBLE_EXTENSIONS:
            entry = {"url": url, "text": filename, "ext": ext}
            return CrawlPageResult(
                task=task,
                status="ok",
                url=url,
                title=filename,
                files=[entry],
                extracted={"files": [entry]},
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                group=task.group,
                extracted_at=now,
            )
        # non-collectible binary/asset — skip silently
        return CrawlPageResult(
            task=task,
            status="ok",
            url=url,
            title=filename,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            group=task.group,
            extracted_at=now,
        )
