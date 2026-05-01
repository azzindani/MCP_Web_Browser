"""Per-domain CSS / XPath extraction selectors.

Each entry maps a domain pattern to a dict of named selectors so the
browser worker can extract structured data without hardcoding selectors
inside the worker itself.

Format::

    SELECTORS: dict[str, dict[str, str]] = {
        "example.com": {
            "title"   : "h1.article-title",
            "price"   : ".stock-price",
            "content" : "article.body",
        },
    }
"""

from __future__ import annotations

SELECTORS: dict[str, dict[str, str]] = {
    # IDX / Bursa Efek Indonesia
    "idx.co.id": {
        "price": ".last-price",
        "change": ".price-change",
        "volume": ".volume",
    },
    # OJK
    "ojk.go.id": {
        "content": ".field-items",
        "title": "h1.page-header",
    },
    # Investing.com Indonesia
    "id.investing.com": {
        "price": "[data-test='instrument-price-last']",
        "change": "[data-test='instrument-price-change']",
        "change_pct": "[data-test='instrument-price-change-percent']",
    },
    # kontan.co.id (financial news)
    "www.kontan.co.id": {
        "title": "h1.title",
        "content": ".tmpt-desk-kon p",
    },
    # bisnis.com
    "www.bisnis.com": {
        "title": "h1.title--detail",
        "content": ".desc--detail p",
    },
}


def get(domain: str) -> dict[str, str]:
    """Return selectors for *domain*, or {} if none are registered."""
    # Exact match first
    if domain in SELECTORS:
        return SELECTORS[domain]
    # Suffix match (subdomain fallback)
    for key, val in SELECTORS.items():
        if domain.endswith("." + key) or domain == key:
            return val
    return {}
