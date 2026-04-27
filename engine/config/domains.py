"""Per-domain rate / timeout / mode overrides.

Lookup is `DOMAIN_CONFIG.get(host, {})`. A `mode: "blocked"` entry tells
the router to skip the domain entirely (e.g. Cloudflare-Turnstile sites
that need a residential proxy we don't have).
"""

from __future__ import annotations

from typing import Final, TypedDict


class DomainOverride(TypedDict, total=False):
    rps: float
    timeout: float
    mode: str


DOMAIN_CONFIG: Final[dict[str, DomainOverride]] = {
    # Finance / Indonesian sites
    "finance.yahoo.com": {"rps": 2.0, "timeout": 15.0},
    "query1.finance.yahoo.com": {"rps": 3.0, "timeout": 10.0},
    "www.cnbcindonesia.com": {"rps": 1.0, "timeout": 20.0},
    "id.investing.com": {"rps": 1.0, "timeout": 20.0},
    "www.ojk.go.id": {"rps": 0.5, "timeout": 30.0},
    "www.bi.go.id": {"rps": 0.5, "timeout": 60.0},
    "www.ksei.co.id": {"rps": 1.0, "timeout": 15.0},
    "www.idx.co.id": {"rps": 0.3, "timeout": 30.0, "mode": "blocked"},
    # Known-safe public domains — CDN-backed, tolerate faster crawling.
    "en.wikipedia.org": {"rps": 2.0},
    "arxiv.org": {"rps": 1.0},
    "developer.mozilla.org": {"rps": 2.0},
    "www.worldbank.org": {"rps": 2.0},
    "www.adb.org": {"rps": 2.0},
    "data.go.id": {"rps": 1.0},
    "playwright.dev": {"rps": 2.0},
    "nodejs.org": {"rps": 2.0},
    # APIs with their own rate limits.
    "api.coingecko.com": {"rps": 0.3, "timeout": 30.0},
}
