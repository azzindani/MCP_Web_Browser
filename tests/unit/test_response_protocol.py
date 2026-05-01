"""Verify the standard response protocol across all engine entry points.

Every tool response must have:
  - "ok" (bool)
  - "op" (str matching tool name)
  - "progress" (non-empty list)
  - "token_estimate" (positive int)

Error responses must additionally have:
  - "hint" (str with tool name)
  - "suggested_next" (list)

Success responses should have "suggested_next" where next steps exist.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import engine
from engine.db.schema import init_schema


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


def _inject(mem_db: sqlite3.Connection) -> engine._Runtime:
    rt = engine.runtime()
    rt._db = mem_db
    return rt


def _assert_protocol(res: dict, op: str, expect_ok: bool) -> None:
    assert isinstance(res.get("ok"), bool), "'ok' must be bool"
    assert res.get("ok") is expect_ok, f"expected ok={expect_ok}, got {res.get('ok')}"
    assert res.get("op") == op, f"expected op={op!r}, got {res.get('op')!r}"
    assert isinstance(res.get("progress"), list), "'progress' must be a list"
    assert len(res["progress"]) > 0, "'progress' must be non-empty"
    assert isinstance(res.get("token_estimate"), int), "'token_estimate' must be int"
    assert res["token_estimate"] > 0, "'token_estimate' must be positive"
    for entry in res["progress"]:
        assert "status" in entry
        assert entry["status"] in ("ok", "fail", "info", "warn", "undo")
    if not expect_ok:
        assert "hint" in res, "error response must have 'hint'"
        assert isinstance(res["hint"], str) and len(res["hint"]) > 10
        assert "suggested_next" in res, "error response must have 'suggested_next'"


# ── browse tier ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_search_success_protocol() -> None:
    mock_hit = MagicMock(title="T", url="https://x.com", snippet="s", backend="ddg")
    mock_result = MagicMock(
        backend="ddg",
        query="q",
        hits=[mock_hit],
        total=1,
        truncated=False,
        elapsed_ms=50,
    )
    with patch.object(engine._Runtime, "search_worker") as f:
        sw = MagicMock()
        sw.search = AsyncMock(return_value=mock_result)
        f.return_value = sw
        res = await engine.search_web("q")
    _assert_protocol(res, "browse_search", expect_ok=True)
    assert "suggested_next" in res
    assert "carry_forward" in res


@pytest.mark.asyncio
async def test_browse_search_failure_protocol() -> None:
    mock_result = MagicMock(
        backend="none",
        query="q",
        hits=[],
        total=0,
        truncated=False,
        elapsed_ms=50,
    )
    with (
        patch.object(engine._Runtime, "search_worker") as f,
        patch.object(engine._Runtime, "browser_search_worker", new=AsyncMock(side_effect=RuntimeError("no browser"))),
    ):
        sw = MagicMock()
        sw.search = AsyncMock(return_value=mock_result)
        f.return_value = sw
        res = await engine.search_web("q")
    _assert_protocol(res, "browse_search", expect_ok=False)


@pytest.mark.asyncio
async def test_browse_fetch_success_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    mock_fetch = MagicMock(
        status="ok",
        url="https://x.com",
        title="X",
        mode="http_curl",
        elapsed_ms=100,
        extracted={"text_preview": "hello"},
        group="",
        ticker=None,
        extracted_at="2025-01-01T00:00:00+00:00",
        error=None,
    )
    with patch.object(engine._Runtime, "http_worker") as f:
        hw = MagicMock()
        hw.fetch_one = AsyncMock(return_value=mock_fetch)
        f.return_value = hw
        res = await engine.fetch_one("https://x.com")
    _assert_protocol(res, "browse_fetch", expect_ok=True)
    assert "suggested_next" in res
    assert "carry_forward" in res


@pytest.mark.asyncio
async def test_browse_fetch_failure_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    mock_fetch = MagicMock(
        status="error",
        url="https://x.com",
        title="",
        mode="http_curl",
        elapsed_ms=50,
        extracted={},
        group="",
        ticker=None,
        extracted_at="2025-01-01T00:00:00+00:00",
        error="HTTP 503",
    )
    with patch.object(engine._Runtime, "http_worker") as f:
        hw = MagicMock()
        hw.fetch_one = AsyncMock(return_value=mock_fetch)
        f.return_value = hw
        res = await engine.fetch_one("https://x.com")
    _assert_protocol(res, "browse_fetch", expect_ok=False)


@pytest.mark.asyncio
async def test_browse_inspect_success_protocol() -> None:
    mock_fetch = MagicMock(
        status="ok",
        url="https://x.com",
        title="X",
        mode="http_curl",
        elapsed_ms=80,
        extracted={"text_preview": "preview text"},
        error=None,
    )
    with patch.object(engine._Runtime, "http_worker") as f:
        hw = MagicMock()
        hw.fetch_one = AsyncMock(return_value=mock_fetch)
        f.return_value = hw
        res = await engine.inspect_one("https://x.com")
    _assert_protocol(res, "browse_inspect", expect_ok=True)


def test_browse_verify_not_indexed_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.verify_one("https://notexist.com")
    _assert_protocol(res, "browse_verify", expect_ok=False)


def test_browse_verify_indexed_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    mem_db.execute(
        "INSERT INTO pages (url, domain, title, status, mode, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
        ("https://x.com", "x.com", "X", "ok", "http_curl", "2025-01-01", "2025-01-01"),
    )
    mem_db.commit()
    res = engine.verify_one("https://x.com")
    _assert_protocol(res, "browse_verify", expect_ok=True)


def test_engine_status_protocol() -> None:
    res = engine.engine_status()
    _assert_protocol(res, "browse_status", expect_ok=True)


# ── query tier ───────────────────────────────────────────────────────────────


def test_query_locate_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_locate()
    _assert_protocol(res, "query_locate", expect_ok=True)


def test_query_stats_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_stats()
    _assert_protocol(res, "query_stats", expect_ok=True)


def test_query_select_success_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_select("SELECT 1 AS n", limit=1)
    _assert_protocol(res, "query_select", expect_ok=True)


def test_query_select_error_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_select("DROP TABLE pages")
    _assert_protocol(res, "query_select", expect_ok=False)


def test_query_search_bad_table_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_search("q", table="nonexistent")
    _assert_protocol(res, "query_search", expect_ok=False)


def test_query_search_success_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_search("hello", table="fts_pages")
    _assert_protocol(res, "query_search", expect_ok=True)


def test_query_export_bad_format_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_export("pages", "/tmp/out", fmt="xml")
    _assert_protocol(res, "query_export", expect_ok=False)


def test_query_export_bad_table_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.query_export("nonexistent", "/tmp/out", fmt="csv")
    _assert_protocol(res, "query_export", expect_ok=False)


# ── crawl tier ──────────────────────────────────────────────────────────────


def test_crawl_verify_not_found_protocol(mem_db: sqlite3.Connection) -> None:
    _inject(mem_db)
    res = engine.crawl_verify("run-nonexistent")
    _assert_protocol(res, "crawl_verify", expect_ok=False)
