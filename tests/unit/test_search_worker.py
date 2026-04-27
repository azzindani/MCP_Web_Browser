"""Verify SearchWorker backend fallback chain (SearXNG → DDG → Brave → none)."""

from __future__ import annotations

import json

import httpx
import pytest

from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.search_worker import SearchWorker


async def _no_sleep(_s: float) -> None:
    return None


def _make_worker(
    handler: httpx.MockTransport,
    *,
    searxng_url: str = "http://stub-searxng/",
) -> SearchWorker:
    client = httpx.AsyncClient(transport=handler, timeout=2.0)
    breaker = CircuitBreaker()
    limiter = RateLimiter(sleep_fn=_no_sleep)
    return SearchWorker(
        client=client, breaker=breaker, limiter=limiter, searxng_url=searxng_url,
    )


@pytest.mark.asyncio
async def test_searxng_first_when_available() -> None:
    body = json.dumps(
        {
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com/a",
                    "content": "snippet text",
                }
            ]
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "stub-searxng" in (request.url.host or ""):
            return httpx.Response(
                status_code=200,
                content=body.encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(503)

    worker = _make_worker(httpx.MockTransport(handler))
    result = await worker.search("hello", limit=5)
    assert result.backend == "searxng"
    assert len(result.hits) == 1
    assert result.hits[0].url == "https://example.com/a"


@pytest.mark.asyncio
async def test_falls_back_to_ddg_when_searxng_fails() -> None:
    ddg_html = """
    <html><body>
      <a class="result__a" href="https://example.org/x">Example X</a>
      <a class="result__snippet">A short summary about X</a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if "stub-searxng" in host:
            return httpx.Response(503)
        if "duckduckgo" in host:
            return httpx.Response(
                status_code=200,
                content=ddg_html.encode("utf-8"),
                headers={"content-type": "text/html"},
            )
        return httpx.Response(503)

    worker = _make_worker(httpx.MockTransport(handler))
    result = await worker.search("hello", limit=3)
    assert result.backend == "ddg"
    assert result.hits[0].url == "https://example.org/x"
    assert "summary" in result.hits[0].snippet


@pytest.mark.asyncio
async def test_returns_none_when_all_fail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    worker = _make_worker(httpx.MockTransport(handler))
    result = await worker.search("anything", limit=5)
    assert result.backend == "none"
    assert result.hits == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_respects_get_max_results_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "1")
    body = json.dumps(
        {
            "results": [
                {"title": f"r{i}", "url": f"https://example.com/{i}", "content": "x"}
                for i in range(50)
            ]
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    worker = _make_worker(httpx.MockTransport(handler))
    result = await worker.search("hello")  # no explicit limit
    # Constrained cap is 10. Backend returned 50 but worker truncates.
    assert len(result.hits) == 10
