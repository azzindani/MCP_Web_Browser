"""Verify browser_worker stealth script + result types.

Real Playwright runs are gated on `MCP_BROWSER_TESTS=1` so CI doesn't
need a Chromium download. The unit tests here cover everything that
doesn't require a live browser.
"""

from __future__ import annotations

import os

import pytest

from engine.workers.browser_worker import (
    BrowserResult,
    BrowserTask,
    build_stealth_script,
)
from engine.workers.fingerprint import pick_profile

INTEGRATION = pytest.mark.skipif(
    os.environ.get("MCP_BROWSER_TESTS") != "1",
    reason="set MCP_BROWSER_TESTS=1 to run real Chromium tests",
)


def test_stealth_script_includes_navigator_overrides() -> None:
    script = build_stealth_script(pick_profile("en-US"))
    assert "Object.defineProperty(navigator, 'webdriver'" in script
    assert "Object.defineProperty(navigator, k," in script
    assert "languages" in script


def test_stealth_script_includes_canvas_noise() -> None:
    script = build_stealth_script(pick_profile("id-ID"))
    assert "HTMLCanvasElement.prototype.toDataURL" in script
    assert "NOISE" in script


def test_stealth_script_includes_webgl_overrides() -> None:
    script = build_stealth_script(pick_profile("en-US"))
    assert "WebGLRenderingContext.prototype.getParameter" in script
    assert "37445" in script  # UNMASKED_VENDOR_WEBGL
    assert "37446" in script  # UNMASKED_RENDERER_WEBGL


def test_stealth_script_picks_id_languages_for_id_locale() -> None:
    script = build_stealth_script(pick_profile("id-ID"))
    assert '"id-ID"' in script
    assert '"en-US"' in script  # secondary fallback


def test_stealth_script_format_does_not_double_brace() -> None:
    """No leftover ``{{`` or ``}}`` in the rendered script."""
    script = build_stealth_script(pick_profile("en-US"))
    assert "{{" not in script
    assert "}}" not in script


def test_browser_task_default_extract_type() -> None:
    t = BrowserTask(url="https://example.com/")
    assert t.extract_type == "auto"
    assert t.max_retries == 3


def test_browser_result_defaults() -> None:
    r = BrowserResult(task=BrowserTask(url="x"), status="ok")
    assert r.mode == "browser"
    assert r.endpoints == []
    assert r.links == []


@INTEGRATION
def test_real_chromium_can_render_data_url() -> None:
    """Smoke test: launch real Chromium and extract a preview from a data URL."""
    import asyncio

    from engine.resilience.circuit_breaker import CircuitBreaker
    from engine.resilience.rate_limiter import RateLimiter
    from engine.workers.browser_worker import BrowserWorker

    async def run() -> str:
        async def no_sleep(_s: float) -> None:
            return None

        worker = await BrowserWorker.launch(
            CircuitBreaker(),
            RateLimiter(sleep_fn=no_sleep),
            locale="en-US",
        )
        try:
            result = await worker.fetch_one(
                BrowserTask(
                    url="data:text/html,<h1>hello</h1><p>world</p>",
                    name="x",
                    extract_type="auto",
                )
            )
        finally:
            await worker.close()
        return result.extracted.get("text_preview", "")

    text = asyncio.run(run())
    assert "hello" in text
