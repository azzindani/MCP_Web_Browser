"""End-to-end pipeline tests. No real network — httpx is mocked.

Exercises the full MCP tool flow through engine entry-points:
  browse_search → browse_fetch → browse_verify → query_search

Marker: e2e.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import engine
from engine.db.schema import init_schema

# ── fixtures ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset() -> Any:
    engine.reset_runtime()
    yield
    engine.reset_runtime()


@pytest.fixture()
def mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _inject_db(mem_db: sqlite3.Connection) -> engine._Runtime:
    rt = engine.runtime()
    rt._db = mem_db
    return rt


# ── browse_search ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_search_returns_hits() -> None:
    mock_hit = MagicMock(
        title="Asyncio docs",
        url="https://docs.python.org/asyncio",
        snippet="The asyncio event loop",
        backend="ddg",
    )
    mock_result = MagicMock(
        backend="ddg",
        query="python asyncio",
        hits=[mock_hit],
        total=1,
        truncated=False,
        elapsed_ms=80,
    )
    with patch.object(engine._Runtime, "search_worker") as factory:
        sw = MagicMock()
        sw.search = AsyncMock(return_value=mock_result)
        factory.return_value = sw
        result = await engine.search_web("python asyncio", limit=10)

    assert result["ok"] is True
    assert len(result["hits"]) == 1
    assert result["hits"][0]["url"] == "https://docs.python.org/asyncio"
    assert result["backend"] == "ddg"


@pytest.mark.asyncio
async def test_browse_search_no_results() -> None:
    mock_result = MagicMock(
        backend="none",
        query="xyzzy",
        hits=[],
        total=0,
        truncated=False,
        elapsed_ms=50,
    )
    with patch.object(engine._Runtime, "search_worker") as factory:
        sw = MagicMock()
        sw.search = AsyncMock(return_value=mock_result)
        factory.return_value = sw
        result = await engine.search_web("xyzzy")

    assert result["ok"] is False
    assert result["hits"] == []


# ── browse_fetch → browse_verify ──────────────────────────────


@pytest.mark.asyncio
async def test_browse_fetch_indexes_and_verify(
    mem_db: sqlite3.Connection,
) -> None:
    _inject_db(mem_db)

    mock_fetch = MagicMock(
        status="ok",
        url="https://example.com/page",
        title="Example Page",
        mode="http_curl",
        elapsed_ms=200,
        extracted={"text_preview": "hello world content"},
        group="",
        ticker=None,
        extracted_at="2025-01-01T00:00:00+00:00",
        error=None,
    )
    with patch.object(engine._Runtime, "http_worker") as factory:
        hw = MagicMock()
        hw.fetch_one = AsyncMock(return_value=mock_fetch)
        factory.return_value = hw
        fetch_result = await engine.fetch_one("https://example.com/page")

    assert fetch_result["ok"] is True
    assert "page" in fetch_result["indexed"]

    verify_result = engine.verify_one("https://example.com/page")
    assert verify_result["ok"] is True
    assert verify_result["title"] == "Example Page"
    assert verify_result["mode"] == "http_curl"


@pytest.mark.asyncio
async def test_browse_fetch_error_not_indexed(
    mem_db: sqlite3.Connection,
) -> None:
    _inject_db(mem_db)

    mock_fetch = MagicMock(
        status="error",
        url="https://broken.com/",
        title="",
        mode="http_curl",
        elapsed_ms=100,
        extracted={},
        group="",
        ticker=None,
        extracted_at="2025-01-01T00:00:00+00:00",
        error="HTTP 503",
    )
    with patch.object(engine._Runtime, "http_worker") as factory:
        hw = MagicMock()
        hw.fetch_one = AsyncMock(return_value=mock_fetch)
        factory.return_value = hw
        result = await engine.fetch_one("https://broken.com/")

    assert result["ok"] is False
    assert result["indexed"] == []

    verify = engine.verify_one("https://broken.com/")
    assert verify["ok"] is False


# ── query tier ───────────────────────────────────────────────


def test_query_locate_lists_tables(mem_db: sqlite3.Connection) -> None:
    _inject_db(mem_db)
    result = engine.query_locate()
    assert result["ok"] is True
    assert "pages" in result["tables"]


def test_query_stats_returns_counts(mem_db: sqlite3.Connection) -> None:
    _inject_db(mem_db)
    result = engine.query_stats()
    assert result["ok"] is True
    assert "pages" in result


def test_query_select_works(mem_db: sqlite3.Connection) -> None:
    _inject_db(mem_db)
    result = engine.query_select("SELECT COUNT(*) AS n FROM pages", limit=1)
    assert result["ok"] is True
    assert result["rows"][0]["n"] == 0


def test_query_select_blocks_drop(mem_db: sqlite3.Connection) -> None:
    _inject_db(mem_db)
    result = engine.query_select("DROP TABLE pages")
    assert result["ok"] is False


def test_query_search_bad_table(mem_db: sqlite3.Connection) -> None:
    _inject_db(mem_db)
    result = engine.query_search("test", table="bad_table")
    assert result["ok"] is False


# ── engine_status ─────────────────────────────────────────────


def test_engine_status_ok() -> None:
    result = engine.engine_status()
    assert result["ok"] is True
    assert "breaker" in result
    assert "limiter" in result


# ── no stdout from engine ─────────────────────────────────────


def test_engine_status_no_stdout(capsys: Any) -> None:
    engine.engine_status()
    captured = capsys.readouterr()
    assert captured.out == "", "engine must not write to stdout"


def test_query_locate_no_stdout(mem_db: sqlite3.Connection, capsys: Any) -> None:
    _inject_db(mem_db)
    engine.query_locate()
    assert capsys.readouterr().out == ""
