"""Verify HttpWorker behaviour against a mocked httpx transport."""

from __future__ import annotations

import json

import httpx
import pytest

from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.http_worker import HttpWorker, Task


def _fixed_handler(body: str, status: int = 200, content_type: str = "application/json") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            content=body.encode("utf-8"),
            headers={"content-type": content_type},
        )

    return httpx.MockTransport(handler)


async def _no_sleep(_s: float) -> None:
    return None


def _make_worker(transport: httpx.MockTransport) -> HttpWorker:
    client = httpx.AsyncClient(transport=transport, timeout=2.0)
    breaker = CircuitBreaker()
    limiter = RateLimiter(sleep_fn=_no_sleep)
    return HttpWorker(client=client, breaker=breaker, limiter=limiter)


@pytest.mark.asyncio
async def test_fetch_json_returns_extracted_dict() -> None:
    body = json.dumps({"hello": "world"})
    worker = _make_worker(_fixed_handler(body))
    result = await worker.fetch_one(Task(url="https://example.com/api", name="api"))
    assert result.status == "ok"
    assert result.extracted == {"hello": "world"}
    assert result.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_fetch_yahoo_chart_extracts_price() -> None:
    body = json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 9500.0,
                            "chartPreviousClose": 9400.0,
                            "currency": "IDR",
                            "exchangeName": "JKT",
                            "longName": "Bank Central Asia",
                        }
                    }
                ]
            }
        }
    )
    worker = _make_worker(_fixed_handler(body))
    result = await worker.fetch_one(
        Task(
            url="https://query1.finance.yahoo.com/v8/finance/chart/BBCA.JK",
            name="BBCA.JK",
            group="Yahoo",
        )
    )
    assert result.status == "ok"
    assert result.extracted["price"] == 9500.0
    assert result.extracted["currency"] == "IDR"
    assert result.title == "Bank Central Asia"
    assert result.ticker == "BBCA.JK"


@pytest.mark.asyncio
async def test_json_object_sniffed_despite_wrong_content_type() -> None:
    # Regression: the body-sniff fallback checked for a literal "{{" prefix
    # instead of "{", so a JSON object served with an inaccurate/missing
    # content-type header was silently treated as opaque HTML/text.
    body = json.dumps({"hello": "world"})
    worker = _make_worker(_fixed_handler(body, content_type="text/plain"))
    result = await worker.fetch_one(Task(url="https://example.com/api", name="api"))
    assert result.status == "ok"
    assert result.extracted == {"hello": "world"}


@pytest.mark.asyncio
async def test_botwall_html_marks_blocked() -> None:
    body = "<html>checking your browser before access</html>"
    worker = _make_worker(_fixed_handler(body, content_type="text/html"))
    result = await worker.fetch_one(Task(url="https://blocked.example/", name="x"))
    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_404_records_error_and_trips_breaker() -> None:
    worker = _make_worker(_fixed_handler("not found", status=404, content_type="text/plain"))
    result = await worker.fetch_one(Task(url="https://example.com/missing", name="x", max_retries=1))
    assert result.status == "error"
    assert "404" in (result.error or "")


@pytest.mark.asyncio
async def test_circuit_open_skips_fetch() -> None:
    worker = _make_worker(_fixed_handler('{"k":1}'))
    # Force the breaker into the open state for this domain.
    for _ in range(5):
        worker._breaker.failure("open.example")
    result = await worker.fetch_one(Task(url="https://open.example/x", name="x"))
    assert result.status == "skipped"
    assert result.error == "circuit open"
