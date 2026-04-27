"""Verify CrawlWorker BFS + file discovery + checkpoint resume."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from engine.core.checkpoint import Checkpoint
from engine.resilience.circuit_breaker import CircuitBreaker
from engine.resilience.rate_limiter import RateLimiter
from engine.workers.crawl_worker import CrawlTask, CrawlWorker


class _FakeClock:
    """Clock + sleep pair that lets the rate limiter refill instantly."""

    def __init__(self) -> None:
        self.t = 0

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds * 1000) + 1


def _site_handler(pages: dict[str, tuple[str, str]]) -> httpx.MockTransport:
    """Build a mock transport from {url: (content_type, body)} mapping."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        page = pages.get(url)
        if page is None:
            return httpx.Response(404, content=b"missing")
        content_type, body = page
        return httpx.Response(
            status_code=200,
            content=body.encode("utf-8"),
            headers={"content-type": content_type},
        )

    return httpx.MockTransport(handler)


def _make_worker(transport: httpx.MockTransport) -> CrawlWorker:
    client = httpx.AsyncClient(transport=transport, timeout=2.0)
    clock = _FakeClock()
    return CrawlWorker(
        client=client,
        breaker=CircuitBreaker(),
        limiter=RateLimiter(now_fn=clock.now, sleep_fn=clock.sleep),
    )


@pytest.mark.asyncio
async def test_bfs_visits_inside_path_prefix() -> None:
    pages = {
        "https://example.com/section/index.html": (
            "text/html",
            """
            <title>Index</title>
            <a href="/section/page-a.html">A</a>
            <a href="/section/page-b.html">B</a>
            <a href="/other/page-c.html">C</a>
            <a href="https://elsewhere.example/x">cross</a>
            """,
        ),
        "https://example.com/section/page-a.html": ("text/html", "<title>A</title>"),
        "https://example.com/section/page-b.html": ("text/html", "<title>B</title>"),
        "https://example.com/other/page-c.html": ("text/html", "<title>C</title>"),
    }
    worker = _make_worker(_site_handler(pages))
    report = await worker.run(
        CrawlTask(
            url="https://example.com/section/index.html",
            crawl_depth=2,
            max_pages=10,
        )
    )
    titles = sorted(p.title for p in report.pages if p.status == "ok")
    # /section/* visited, /other/* and cross-domain skipped
    assert titles == ["A", "B", "Index"]
    assert report.errors == 0


@pytest.mark.asyncio
async def test_collects_pdf_link_as_file() -> None:
    pages = {
        "https://example.com/data/index.html": (
            "text/html",
            """
            <title>Data</title>
            <a href="/data/report.pdf">Report</a>
            <a href="/data/other.html">More</a>
            """,
        ),
        "https://example.com/data/other.html": (
            "text/html",
            "<title>Other</title>",
        ),
    }
    worker = _make_worker(_site_handler(pages))
    report = await worker.run(
        CrawlTask(
            url="https://example.com/data/index.html",
            crawl_depth=1,
            max_pages=5,
        )
    )
    seed = next(p for p in report.pages if p.url.endswith("/index.html"))
    file_urls = [f["url"] for f in seed.files]
    assert "https://example.com/data/report.pdf" in file_urls


@pytest.mark.asyncio
async def test_max_pages_cap_is_respected() -> None:
    pages: dict[str, tuple[str, str]] = {
        "https://example.com/p/0.html": (
            "text/html",
            "".join(
                f'<a href="/p/{i}.html">{i}</a>' for i in range(1, 20)
            ) + "<title>0</title>",
        ),
    }
    for i in range(1, 20):
        pages[f"https://example.com/p/{i}.html"] = (
            "text/html", f"<title>{i}</title>"
        )

    worker = _make_worker(_site_handler(pages))
    report = await worker.run(
        CrawlTask(
            url="https://example.com/p/0.html",
            crawl_depth=2,
            max_pages=5,
        )
    )
    assert len(report.pages) == 5


@pytest.mark.asyncio
async def test_resume_skips_done_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    pages = {
        "https://example.com/r/index.html": (
            "text/html",
            '<title>R</title><a href="/r/a.html">A</a><a href="/r/b.html">B</a>',
        ),
        "https://example.com/r/a.html": ("text/html", "<title>A</title>"),
        "https://example.com/r/b.html": ("text/html", "<title>B</title>"),
    }
    cp = Checkpoint("ckpt.json", run_id="resume-1")
    cp.save({"https://example.com/r/a.html"}, total_count=2)

    worker = _make_worker(_site_handler(pages))
    report = await worker.run(
        CrawlTask(url="https://example.com/r/index.html", crawl_depth=1, max_pages=5),
        checkpoint=cp,
    )
    # Index is the seed (always crawled). 'A' must be skipped, 'B' must run.
    fetched_urls = [p.url for p in report.pages]
    assert "https://example.com/r/a.html" not in fetched_urls
    assert "https://example.com/r/b.html" in fetched_urls
