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
