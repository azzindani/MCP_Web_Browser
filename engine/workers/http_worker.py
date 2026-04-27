"""HTTP worker — handles `http_json` and `http_curl` modes.

Uses `httpx` async with HTTP/2. Bot-wall detection upgrades the result
to `status="blocked"` so the scheduler can re-route via the browser
worker. Yahoo Finance chart payloads are auto-parsed to a flat extracted
dict so the indexer's stock router can pick them up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from engine.config.defaults import BOT_BODY_PATTERNS, DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.resilience.retry import with_retry

_BASE_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULTS.USER_AGENT,
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
}

_CURL_HEADERS: dict[str, str] = {
    **_BASE_HEADERS,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": (
        '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    links: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    group: str = ""
    ticker: str | None = None
    extracted_at: str = ""
