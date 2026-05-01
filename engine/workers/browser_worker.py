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
from datetime import UTC, datetime
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
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    languages = ["id-ID", "id", "en-US", "en"] if p.locale.startswith("id") else ["en-US", "en"]
    platform_value = "Windows" if p.platform.startswith("Win") else "macOS" if p.platform == "MacIntel" else "Linux"
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


# Doubled `{{` `}}` escapes are required by str.format below.
_STEALTH_TEMPLATE = """
(function() {{
  // Remove automation markers
  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  delete window.__nightmare;
  delete window._phantom;
  delete window.callPhantom;
  Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {{
    try {{ delete window[k]; }} catch (e) {{}}
  }});

  const overrides = {{
    platform: {platform},
    hardwareConcurrency: {hardware},
    deviceMemory: {memory},
    maxTouchPoints: {touch},
    languages: {languages},
  }};
  for (const [k, v] of Object.entries(overrides)) {{
    try {{
      Object.defineProperty(navigator, k, {{ get: () => v, configurable: true }});
    }} catch (e) {{}}
  }}

  Object.defineProperty(navigator, 'plugins', {{
    get: () => {{
      const arr = [
        {{ filename: 'internal-pdf-viewer', description: 'Portable Document Format', name: 'Chrome PDF Plugin' }},
        {{ filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format', name: 'Chrome PDF Viewer' }},
        {{ filename: 'internal-nacl-plugin', description: 'Native Client', name: 'Native Client' }},
      ];
      arr.refresh = () => {{}};
      arr.item = (i) => arr[i] ?? null;
      arr.namedItem = (n) => arr.find(p => p.name === n) ?? null;
      return arr;
    }},
    configurable: true,
  }});

  window.chrome = window.chrome || {{}};
  window.chrome.runtime = window.chrome.runtime || {{
    connect: () => ({{}}),
    sendMessage: () => {{}},
    onMessage: {{ addListener: () => {{}} }},
    id: undefined,
  }};
  window.chrome.loadTimes = () => ({{}});
  window.chrome.csi = () => ({{}});
  window.chrome.app = {{}};

  try {{
    Object.defineProperty(screen, 'width',       {{ get: () => {screen_w} }});
    Object.defineProperty(screen, 'height',      {{ get: () => {screen_h} }});
    Object.defineProperty(screen, 'availWidth',  {{ get: () => {screen_w} }});
    Object.defineProperty(screen, 'availHeight', {{ get: () => {avail_h} }});
    Object.defineProperty(screen, 'colorDepth',  {{ get: () => {color} }});
    Object.defineProperty(screen, 'pixelDepth',  {{ get: () => {color} }});
  }} catch (e) {{}}

  if (window.outerWidth === 0) {{
    Object.defineProperty(window, 'outerWidth',  {{ get: () => window.innerWidth }});
    Object.defineProperty(window, 'outerHeight', {{ get: () => window.innerHeight }});
  }}

  const NOISE = {canvas_noise};
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
  HTMLCanvasElement.prototype.toDataURL = function (...args) {{
    const ctx = this.getContext('2d');
    if (ctx) {{
      const imgData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
      for (let i = 0; i < imgData.data.length; i += 4) {{
        imgData.data[i] = Math.min(255, imgData.data[i] + NOISE * 255);
        imgData.data[i + 1] = Math.min(255, imgData.data[i + 1] + NOISE * 255);
        imgData.data[i + 2] = Math.min(255, imgData.data[i + 2] + NOISE * 255);
      }}
      ctx.putImageData(imgData, 0, 0);
    }}
    return _toDataURL.apply(this, args);
  }};
  CanvasRenderingContext2D.prototype.getImageData = function (...args) {{
    const data = _getImageData.apply(this, args);
    for (let i = 0; i < data.data.length; i += 4) {{
      data.data[i] = Math.min(255, data.data[i] + (Math.random() - 0.5) * NOISE * 255);
      data.data[i + 1] = Math.min(255, data.data[i + 1] + (Math.random() - 0.5) * NOISE * 255);
      data.data[i + 2] = Math.min(255, data.data[i + 2] + (Math.random() - 0.5) * NOISE * 255);
    }}
    return data;
  }};

  const WGL_VENDOR = {webgl_vendor};
  const WGL_RENDERER = {webgl_renderer};
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {{
    if (param === 37445) return WGL_VENDOR;
    if (param === 37446) return WGL_RENDERER;
    return _getParam.call(this, param);
  }};
  if (typeof WebGL2RenderingContext !== 'undefined') {{
    const _g2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {{
      if (param === 37445) return WGL_VENDOR;
      if (param === 37446) return WGL_RENDERER;
      return _g2.call(this, param);
    }};
  }}

  if ('getBattery' in navigator) {{
    navigator.getBattery = () => Promise.resolve({{
      charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1.0,
      addEventListener: () => {{}}, removeEventListener: () => {{}}, dispatchEvent: () => true,
    }});
  }}

  if (navigator.permissions) {{
    const _query = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {{
      const grant = ['notifications', 'clipboard-read', 'clipboard-write'];
      if (grant.includes(params.name)) {{
        return Promise.resolve({{ state: 'granted', onchange: null,
          addEventListener: () => {{}}, removeEventListener: () => {{}},
          dispatchEvent: () => true }});
      }}
      return _query(params);
    }};
  }}

  if (!navigator.connection) {{
    Object.defineProperty(navigator, 'connection', {{
      get: () => ({{ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false }}),
      configurable: true,
    }});
  }}
}})();
"""


# ── Per-extract-type DOM evaluators (returned as JS strings) ───────────

_EVAL_HEADLINES = """
() => {
  const headlines = [];
  for (const art of Array.from(document.querySelectorAll('article')).slice(0, 20)) {
    for (const sel of ['h2','h3','h4','.title','.headline']) {
      const el = art.querySelector(sel);
      const text = (el && (el.innerText || el.textContent) || '').trim();
      if (text.length > 8) { headlines.push(text.slice(0, 120)); break; }
    }
  }
  if (headlines.length < 5) {
    const seen = new Set(headlines);
    const cand = Array.from(document.querySelectorAll(
      "h2 a, h3 a, h4 a, [class*='title'] a, [class*='headline'] a"
    )).slice(0, 30);
    for (const el of cand) {
      const t = (el.innerText || el.textContent || '').trim();
      if (t.length > 8 && !seen.has(t)) { seen.add(t); headlines.push(t.slice(0, 120)); }
    }
  }
  return { headlines: headlines.slice(0, 15), count: headlines.length };
}
"""

_EVAL_STOCK = """
() => {
  const get = (field) =>
    document.querySelector("fin-streamer[data-field='" + field + "']")
      ?.getAttribute('value') ?? null;
  const nameEl = document.querySelector("h1[class*='title']")
    ?? document.querySelector('section h1');
  return {
    price: get('regularMarketPrice'),
    change: get('regularMarketChange'),
    changePct: get('regularMarketChangePercent'),
    volume: get('regularMarketVolume'),
    marketCap: get('marketCap'),
    dayHigh: get('regularMarketDayHigh'),
    dayLow: get('regularMarketDayLow'),
    prevClose: get('regularMarketPreviousClose'),
    week52High: get('fiftyTwoWeekHigh'),
    week52Low: get('fiftyTwoWeekLow'),
    companyName: (nameEl && (nameEl.innerText || nameEl.textContent) || '').trim() || null,
  };
}
"""

_EVAL_INDEX = """
() => {
  const pickFirst = (sels) => {
    for (const sel of sels) {
      const el = document.querySelector(sel);
      const t = (el && (el.innerText || el.textContent) || '').trim();
      if (t && /[\\d,.]/.test(t)) return t;
    }
    return null;
  };
  return {
    price: pickFirst([
      "[data-test='instrument-price-last']", "[class*='last-price']",
      "[class*='lastPrice']", "[class*='priceSection'] [class*='price']",
      "span[class*='text-5xl']", ".instrument-price_last-price",
    ]),
    change: pickFirst([
      "[data-test='instrument-price-change']",
      "[class*='price-change'] [class*='change']",
    ]),
    changePct: pickFirst([
      "[data-test='instrument-price-change-percent']",
      "[class*='price-change'] [class*='percent']",
    ]),
  };
}
"""

_EVAL_PREVIEW = """
() => {
  const body = document.body && (document.body.innerText || document.body.textContent) || '';
  return { text_preview: body.slice(0, 500).replace(/\\n/g, ' ').trim() };
}
"""

_EVAL_LINKS = """
() => Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.href)
        .filter(h => h && h.startsWith('http'))
        .slice(0, 50)
"""


_OVERLAY_CLICK_SELECTORS = (
    "button[name='agree']",
    "#onetrust-accept-btn-handler",
    "button[id*='accept']",
    ".js-accept-cookies",
    "[data-testid='sign-in-bar-close']",
    ".popupCloseIcon",
)


class BrowserWorker:
    """Async Playwright worker. Construct via `await BrowserWorker.launch()`."""

    def __init__(
        self,
        playwright: Any,
        browser: Any,
        breaker: CircuitBreaker,
        limiter: RateLimiter,
        profile: BrowserProfile,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._breaker = breaker
        self._limiter = limiter
        self._profile = profile

    @classmethod
    async def launch(
        cls,
        breaker: CircuitBreaker,
        limiter: RateLimiter,
        *,
        locale: str = DEFAULTS.LOCALE,
    ) -> BrowserWorker:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=list(DEFAULTS.CHROMIUM_ARGS))
        return cls(
            playwright=pw,
            browser=browser,
            breaker=breaker,
            limiter=limiter,
            profile=pick_profile(locale),
        )

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._playwright.stop()

    async def fetch_one(self, task: BrowserTask) -> BrowserResult:
        domain = _domain_of(task.url)
        now = _now_iso()
        t0 = time.monotonic()
        timeout = DOMAIN_CONFIG.get(domain, {}).get("timeout", DEFAULTS.BROWSER_TIMEOUT)

        if not self._breaker.allow(domain):
            return BrowserResult(
                task=task,
                status="blocked",
                url=task.url,
                group=task.group,
                extract_type=task.extract_type,
                extracted_at=now,
                error="circuit open",
            )

        await self._limiter.acquire(domain)

        context = await self._make_context()
        page = await context.new_page()
        await page.add_init_script(build_stealth_script(self._profile))
        captured: list[dict[str, Any]] = []
        page.on("dialog", lambda d: d.dismiss())
        page.on("response", lambda r: self._capture_endpoint(r, captured))

        try:
            await with_retry(
                lambda: page.goto(
                    task.url,
                    wait_until="domcontentloaded",
                    timeout=timeout * 1000,
                ),
                max_retries=task.max_retries,
            )
            title = await page.title()
            if any(c in title.lower() for c in DEFAULTS.CHALLENGE_TITLES):
                await context.close()
                self._breaker.failure(domain)
                return BrowserResult(
                    task=task,
                    status="blocked",
                    url=task.url,
                    title=title,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error="Cloudflare challenge not cleared",
                    group=task.group,
                    extract_type=task.extract_type,
                    extracted_at=now,
                )

            await self._dismiss_overlays(page)
            extracted = await self._extract(page, task.extract_type)
            links = await page.evaluate(_EVAL_LINKS)

            await context.close()
            self._breaker.success(domain)

            return BrowserResult(
                task=task,
                status="ok",
                url=task.url,
                title=title,
                extracted=extracted,
                links=list(links),
                endpoints=captured,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                group=task.group,
                extract_type=task.extract_type,
                extracted_at=now,
            )
        except Exception as exc:
            try:
                await context.close()
            except Exception:  # noqa: S110
                pass
            self._breaker.failure(domain)
            msg = str(exc)
            is_timeout = "timeout" in msg.lower()
            return BrowserResult(
                task=task,
                status="timeout" if is_timeout else "error",
                url=task.url,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=msg[:100],
                endpoints=captured,
                group=task.group,
                extract_type=task.extract_type,
                extracted_at=now,
            )

    async def _make_context(self) -> Any:
        p = self._profile
        return await self._browser.new_context(
            viewport={"width": p.viewport[0], "height": p.viewport[1]},
            user_agent=p.user_agent,
            locale=p.locale,
            timezone_id=p.timezone,
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": p.accept_language,
                "sec-ch-ua": p.sec_ch_ua_full,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": json.dumps(
                    "Windows" if p.platform.startswith("Win") else "macOS" if p.platform == "MacIntel" else "Linux"
                ),
            },
        )

    async def _capture_endpoint(self, response: Any, sink: list[dict[str, Any]]) -> None:
        url = response.url
        if "umbraco/Surface" in url or "/api/" in url:
            try:
                body = await response.json()
                if isinstance(body, dict):
                    sink.append(
                        {
                            "url": url,
                            "method": "GET",
                            "response_keys": list(body.keys()),
                        }
                    )
            except Exception:  # noqa: S110
                pass

    async def _dismiss_overlays(self, page: Any) -> None:
        for sel in _OVERLAY_CLICK_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=800)
                if el:
                    await el.click()
                    await page.wait_for_timeout(300)
            except Exception:  # noqa: S112
                continue

    async def _extract(self, page: Any, extract_type: str) -> dict[str, Any]:
        eval_js = {
            "stock_price": _EVAL_STOCK,
            "headlines": _EVAL_HEADLINES,
            "index_price": _EVAL_INDEX,
        }.get(extract_type, _EVAL_PREVIEW)
        try:
            return await page.evaluate(eval_js)
        except Exception:
            return {}
