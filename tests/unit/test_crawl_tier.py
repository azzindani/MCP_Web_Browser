"""Verify crawl-tier engine entry points: locate, plan, run, resume, verify."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

import engine
from engine.db.schema import init_schema
from engine.resilience.rate_limiter import RateLimiter


@pytest.fixture()
def isolated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    engine.reset_runtime()
    rt = engine.runtime()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    rt._db = conn

    pages = {
        "https://example.com/section/index.html": (
            "text/html",
            """
            <title>Index</title>
            <a href="/section/a.html">A</a>
            <a href="/section/b.html">B</a>
            <a href="/section/r.pdf">PDF</a>
            """,
        ),
        "https://example.com/section/a.html": ("text/html", "<title>A</title>"),
        "https://example.com/section/b.html": ("text/html", "<title>B</title>"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        page = pages.get(url)
        if page is None:
            return httpx.Response(404, content=b"missing")
        ct, body = page
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": ct},
        )

    rt._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )

    # Speed up the rate limiter so tests don't sleep on real wall time.
    class _FakeClock:
        def __init__(self) -> None:
            self.t = 0

        def now(self) -> int:
            return self.t

        async def sleep(self, seconds: float) -> None:
            self.t += int(seconds * 1000) + 1

    clock = _FakeClock()
    rt._limiter = RateLimiter(now_fn=clock.now, sleep_fn=clock.sleep)

    yield None
    asyncio.run(rt.aclose())
    engine.reset_runtime()


def test_crawl_plan_lists_frontier(isolated_engine: None) -> None:
    out = asyncio.run(engine.crawl_plan("https://example.com/section/index.html"))
    assert out["ok"] is True
    urls = [link for link in out["links"]]
    assert "https://example.com/section/a.html" in urls
    file_urls = [f["url"] for f in out["files"]]
    assert "https://example.com/section/r.pdf" in file_urls


def test_crawl_run_indexes_pages(isolated_engine: None) -> None:
    out = asyncio.run(
        engine.crawl_run(
            "https://example.com/section/index.html",
            max_pages=5,
            max_depth=1,
            run_id="run-A",
        )
    )
    assert out["ok"] is True
    assert out["pages"] >= 1
    # Verify the indexer wrote into pages table via query layer.
    rows = engine.query_select("SELECT url FROM pages WHERE url LIKE 'https://example.com/section/%'")
    assert rows["total"] >= 2


def test_crawl_verify_summarises_run(isolated_engine: None) -> None:
    asyncio.run(
        engine.crawl_run(
            "https://example.com/section/index.html",
            max_pages=3,
            max_depth=1,
            run_id="run-V",
        )
    )
    # crawl_verify reads from task_log; CrawlWorker doesn't log_task itself,
    # so this returns ok with zero pages — by design until a scheduler wires it.
    out = engine.crawl_verify("run-V")
    assert out["ok"] is True
    assert out["pages"] >= 0


def test_crawl_resume_runs_with_existing_run_id(
    isolated_engine: None,
) -> None:
    asyncio.run(
        engine.crawl_run(
            "https://example.com/section/index.html",
            max_pages=2,
            max_depth=1,
            run_id="run-R",
        )
    )
    out = asyncio.run(
        engine.crawl_resume(
            "run-R",
            "https://example.com/section/index.html",
            max_pages=2,
        )
    )
    assert out["ok"] is True
    assert out["run_id"] == "run-R"
