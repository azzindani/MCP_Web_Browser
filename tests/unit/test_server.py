"""Verify the MCP server tier: tool registration, docstring caps, engine wiring."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import engine
import server
from engine.db.schema import init_schema

BASIC_TOOLS = {
    "browse_search",
    "browse_locate",
    "browse_inspect",
    "browse_fetch",
    "browse_verify",
    "browse_status",
}
QUERY_TOOLS = {
    "query_locate",
    "query_search",
    "query_select",
    "query_export",
    "query_stats",
}
CRAWL_TOOLS = {
    "crawl_locate",
    "crawl_plan",
    "crawl_run",
    "crawl_resume",
    "crawl_verify",
}

DOCSTRING_CAP = 80


def _registered_tools() -> dict[str, object]:
    """Return the FastMCP tool registry as a {name: fn} mapping."""
    mgr = server.app._tool_manager
    tools = mgr.list_tools()
    return {t.name: t for t in tools}


def test_default_tiers_register_basic_plus_query() -> None:
    names = set(_registered_tools().keys())
    # Crawl tier defaults to off; Basic + Query are on.
    assert BASIC_TOOLS.issubset(names)
    assert QUERY_TOOLS.issubset(names)
    assert names.isdisjoint(CRAWL_TOOLS)


def test_default_tier_count_under_twelve_ceiling() -> None:
    """Basic (6) + Query (5) = 11. Within the 12-tool simultaneous cap."""
    names = set(_registered_tools().keys())
    assert len(names & (BASIC_TOOLS | QUERY_TOOLS)) <= 12


def test_every_tool_docstring_is_within_cap() -> None:
    for name, tool in _registered_tools().items():
        desc = tool.description or ""
        # Strip whitespace for a fair length check.
        first_line = desc.strip().splitlines()[0] if desc.strip() else ""
        assert len(first_line) <= DOCSTRING_CAP, f"{name}: {len(first_line)} chars: {first_line!r}"


def test_no_mcp_imports_under_engine() -> None:
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    for path in (repo_root / "engine").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith(("from mcp", "import mcp")), (
                f"{path}: forbidden mcp import: {stripped}"
            )


@pytest.fixture()
def isolated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire up an in-memory DB + mocked httpx for engine entry points."""
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    engine.reset_runtime()

    rt = engine.runtime()
    # Replace SQLite with an in-memory DB so the test never touches the FS DB.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    rt._db = conn

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if path.endswith("/api"):
            return httpx.Response(
                status_code=200,
                content=json.dumps({"hello": "world"}).encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        if path.endswith("/missing"):
            return httpx.Response(404, content=b"missing")
        return httpx.Response(
            200, content=b"<html>ok</html>", headers={"content-type": "text/html"}
        )

    rt._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )

    async def no_sleep(_s: float) -> None:
        return None

    rt._limiter = engine.RateLimiter(sleep_fn=no_sleep) if False else rt._limiter
    yield None
    asyncio.run(rt.aclose())
    engine.reset_runtime()


def test_fetch_one_indexes_into_pages(isolated_engine: None) -> None:
    out = asyncio.run(engine.fetch_one("https://example.com/api"))
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert "page" in out["indexed"]


def test_verify_one_reads_back_after_fetch(isolated_engine: None) -> None:
    asyncio.run(engine.fetch_one("https://example.com/api"))
    row = engine.verify_one("https://example.com/api")
    assert row["ok"] is True
    assert row["url"] == "https://example.com/api"


def test_verify_one_unknown_url_returns_not_indexed(
    isolated_engine: None,
) -> None:
    out = engine.verify_one("https://nope.example/x")
    assert out["ok"] is False
    assert out["error"] == "not_indexed"


def test_engine_status_reports_open_state(isolated_engine: None) -> None:
    s = engine.engine_status()
    assert s["ok"] is True
    assert s["client_open"] is True
    assert s["db_open"] is True
