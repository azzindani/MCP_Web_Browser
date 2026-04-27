"""Playwright worker — handles SPA pages, JS-rendered content, XHR capture.

Stealth init script is parameterised by a `BrowserProfile` (see
`fingerprint.py`) so navigator / canvas / WebGL / battery / permissions
markers all line up with the chosen UA. Adaptive selectors from krawl
are deferred to a later milestone; M4 ships baseline extraction only.

The worker is async. Tests that exercise real Chromium are gated on
`MCP_BROWSER_TESTS=1`; the unit tests below mock the page object.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from engine.config.defaults import DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.resilience.retry import with_retry
from engine.workers.fingerprint import BrowserProfile, pick_profile


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class BrowserTask:
    url: str
    name: str = ""
    group: str = ""
    extract_type: str = "auto"  # auto | stock_price | headlines | index_price
    max_retries: int = DEFAULTS.MAX_RETRIES


@dataclass
class BrowserResult:
    task: BrowserTask
    status: str  # ok | error | blocked | timeout
    mode: str = "browser"
    url: str = ""
    title: str = ""
    extracted: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    group: str = ""
    extract_type: str = "auto"
    extracted_at: str = ""


def build_stealth_script(p: BrowserProfile) -> str:
    """Return the JS that overrides automation markers / canvas / WebGL.

    Run once per page via `page.add_init_script()` before any site JS.
    """
    languages = (
        ["id-ID", "id", "en-US", "en"]
        if p.locale.startswith("id")
        else ["en-US", "en"]
    )
    platform_value = (
        "Windows" if p.platform.startswith("Win")
        else "macOS" if p.platform == "MacIntel"
        else "Linux"
    )
    avail_height = p.screen_height - 40
    return _STEALTH_TEMPLATE.format(
        platform=json.dumps(p.platform),
        hardware=p.hardware_concurrency,
        memory=p.device_memory,
        touch=p.max_touch_points,
        languages=json.dumps(languages),
        screen_w=p.screen_width,
        screen_h=p.screen_height,
        avail_h=avail_height,
        color=p.color_depth,
        canvas_noise=p.canvas_noise,
        webgl_vendor=json.dumps(p.webgl_vendor),
        webgl_renderer=json.dumps(p.webgl_renderer),
        platform_short=json.dumps(platform_value),
    )
