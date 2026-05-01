"""URL mode detection — the brain of the engine.

Probes a URL once, picks the best fetch mode, and caches the decision
per domain so subsequent calls are instant. Cache is persisted to a
JSON file via atomic rename so crashes never corrupt it.

Mirrors core/router.ts in krawl. Uses httpx instead of Node fetch.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from engine.config.defaults import BOT_BODY_PATTERNS, DEFAULTS
from engine.config.domains import DOMAIN_CONFIG
from shared.path_safety import resolve_path
from shared.version_control import atomic_write_text, snapshot


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── probe data structures ──────────────────────────────────────────


@dataclass
class ProbeSignals:
    status_code: int = 0
    http_works: bool = False
    is_json: bool = False
    is_spa: bool = False
    is_static_html: bool = False
    cloudflare_block: bool = False
    turnstile: bool = False
    bot_wall: bool = False
    bot_wall_service: str = ""
    content_type: str = ""
    response_time_ms: int = 0


@dataclass
class CachedEntry:
    mode: str
    signals: dict[str, Any] = field(default_factory=dict)
    cached_at: str = field(default_factory=_now_iso)
    sample_url: str = ""
    hit_count: int = 0


# ── bot-wall detection (mirrors router.ts detectBotWall) ───────────────


def _detect_bot_wall(body_lower: str, headers: httpx.Headers) -> str:
    if headers.get("x-datadome-cid") or "datadome" in body_lower:
        return "DataDome"
    if "perimeterx" in body_lower or "_pxhd" in body_lower:
        return "PerimeterX"
    if ("akamai" in body_lower and "bot" in body_lower) or headers.get("x-check-cacheable"):
        return "Akamai"
    if "kasada" in body_lower or "kpsdk" in body_lower:
        return "Kasada"
    if "incapsula" in body_lower or "imperva" in body_lower or headers.get("x-iinfo"):
        return "Imperva"
    if any(p in body_lower for p in BOT_BODY_PATTERNS):
        return "generic"
    return ""


# ── Router ─────────────────────────────────────────────────────────


class Router:
    """Probe URLs, cache mode decisions per domain, upgrade on failure."""

    def __init__(self, cache_file: str | Path = "router_cache.json") -> None:
        self._cache_path = resolve_path(cache_file)
        self._cache: dict[str, CachedEntry] = self._load_cache()

    # ── cache persistence ───────────────────────────────────────

    def _load_cache(self) -> dict[str, CachedEntry]:
        if not self._cache_path.exists():
            return {}
        try:
            raw: dict[str, Any] = json.loads(self._cache_path.read_text(encoding="utf-8"))
            _stderr(f"router cache: {len(raw)} domains loaded")
            return {
                domain: CachedEntry(
                    mode=entry["mode"],
                    signals=entry.get("signals", {}),
                    cached_at=entry.get("cached_at", ""),
                    sample_url=entry.get("sample_url", ""),
                    hit_count=int(entry.get("hit_count", 0)),
                )
                for domain, entry in raw.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}

    def _save_cache(self) -> None:
        snapshot(self._cache_path)
        data = {
            domain: {
                "mode": e.mode,
                "signals": e.signals,
                "cached_at": e.cached_at,
                "sample_url": e.sample_url,
                "hit_count": e.hit_count,
            }
            for domain, e in self._cache.items()
        }
        atomic_write_text(self._cache_path, json.dumps(data, indent=2))

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def domain(url: str) -> str:
        try:
            return urlparse(url).hostname or url
        except ValueError:
            return url

    def _hardcoded_mode(self, url: str) -> str | None:
        return DOMAIN_CONFIG.get(self.domain(url), {}).get("mode")

    # ── probe ───────────────────────────────────────────────────────

    async def probe(self, url: str) -> ProbeSignals:
        """Live GET probe — used only on first visit to a domain."""
        t0 = time.monotonic()
        signals = ProbeSignals()
        try:
            async with httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=10.0,
                headers={
                    "User-Agent": DEFAULTS.USER_AGENT,
                    "Accept": "text/html,application/json,*/*",
                    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
                },
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError:
            signals.response_time_ms = int((time.monotonic() - t0) * 1000)
            return signals

        signals.response_time_ms = int((time.monotonic() - t0) * 1000)
        signals.status_code = response.status_code
        ct = response.headers.get("content-type", "").lower()
        signals.content_type = ct

        text = response.text
        lower = text.lower()

        signals.cloudflare_block = response.status_code in (403, 503) and (
            "cloudflare" in lower or any(t in lower for t in DEFAULTS.CHALLENGE_TITLES)
        )
        signals.turnstile = signals.cloudflare_block and (
            "cf-turnstile" in lower or "challenges.cloudflare.com/turnstile" in lower
        )
        svc = _detect_bot_wall(lower, response.headers)
        signals.bot_wall = bool(svc)
        signals.bot_wall_service = svc

        signals.is_json = "json" in ct or lower.lstrip().startswith(("{{", "[["))
        signals.is_spa = "html" in ct and not signals.cloudflare_block and any(m in lower for m in DEFAULTS.SPA_MARKERS)
        signals.is_static_html = (
            "html" in ct
            and not signals.cloudflare_block
            and not signals.is_spa
            and response.status_code == 200
            and len(text) > 5_000
        )
        signals.http_works = response.status_code == 200 and not signals.cloudflare_block and not signals.bot_wall
        return signals

    # ── decide & resolve ───────────────────────────────────────────

    @staticmethod
    def _decide(s: ProbeSignals) -> str:
        if s.turnstile:
            return "blocked"
        if s.cloudflare_block:
            return "browser"  # try browser before giving up
        if s.bot_wall:
            return "browser"  # DataDome/PX/Akamai — browser may pass
        if s.is_json and s.http_works:
            return "http_json"
        if s.is_spa:
            return "browser"
        if s.is_static_html and s.http_works:
            return "crawl"
        if not s.http_works:
            return "browser"
        return "browser"

    async def resolve(self, url: str, force_probe: bool = False) -> str:
        """Return mode for *url*, probing the domain if needed."""
        hardcoded = self._hardcoded_mode(url)
        if hardcoded:
            return hardcoded
        d = self.domain(url)
        if not force_probe and d in self._cache:
            self._cache[d].hit_count += 1
            return self._cache[d].mode
        signals = await self.probe(url)
        mode = self._decide(signals)
        self._cache[d] = CachedEntry(
            mode=mode,
            signals={
                "status_code": signals.status_code,
                "http_works": signals.http_works,
                "is_json": signals.is_json,
                "is_spa": signals.is_spa,
                "cloudflare_block": signals.cloudflare_block,
                "bot_wall_service": signals.bot_wall_service,
                "response_time_ms": signals.response_time_ms,
            },
            cached_at=_now_iso(),
            sample_url=url,
        )
        self._save_cache()
        return mode

    def get_cached_mode(self, url: str) -> str | None:
        entry = self._cache.get(self.domain(url))
        return entry.mode if entry else None

    def upgrade_mode(self, url: str, new_mode: str) -> None:
        """Upgrade cached mode (e.g. http_curl → browser after failures)."""
        d = self.domain(url)
        existing = self._cache.get(d)
        if existing and existing.mode != new_mode:
            _stderr(f"router: {d} upgraded {existing.mode} → {new_mode}")
            existing.mode = new_mode
            self._save_cache()

    def dump_cache(self) -> dict[str, Any]:
        return {d: {"mode": e.mode, "hit_count": e.hit_count} for d, e in self._cache.items()}
