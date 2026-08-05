"""MCP server entrypoint. THIN. One-liner tools only.

Tiers (each toggled by env var, default-on except crawl):

    MCP_TIER_BASIC=1   →  browse_*        (8 tools)
    MCP_TIER_QUERY=1   →  query_*         (5 tools)
    MCP_TIER_CRAWL=0   →  crawl_*         (5 tools, off by default)

At most two tiers should be enabled at once on a constrained host so the
combined schema budget stays within the model's context window. Engine
logic lives in engine/**; this file only validates and dispatches.
"""

from __future__ import annotations

import argparse
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import engine
from deploy_auth import build_auth
from shared.platform_utils import (
    get_research_breadth,
    get_research_depth,
    get_research_fetch_top,
    get_search_limit,
)

_VERSION = "0.1.0"  # keep in sync with pyproject.toml [project].version
_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
_PORT = int(os.environ.get("WEB_PORT", "8766"))
_token_verifier, _auth_settings = build_auth("WEB", _HOST, _PORT)

app: FastMCP = FastMCP(
    "mcp_web_browser",
    host=_HOST,
    port=_PORT,
    token_verifier=_token_verifier,
    auth=_auth_settings,
)


@app.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness check. Unauthenticated."""
    return JSONResponse({"status": "ok", "version": _VERSION})


@app.custom_route("/version", methods=["GET"])
async def version(request: Request) -> JSONResponse:
    """Report running version. Unauthenticated."""
    return JSONResponse({"current": _VERSION})


def _enabled(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


# ── Basic tier ─────────────────────────────────────────────────────

if _enabled("MCP_TIER_BASIC", "1"):

    @app.tool()
    async def browse_search(
        query: str,
        limit: Annotated[
            int | None, Field(default=None, ge=1, le=50, description="Max hits (default: MCP_SEARCH_LIMIT)")
        ] = None,
    ) -> dict[str, Any]:
        """Web search (SearXNG/DDG/Brave). Returns <=10 hits."""
        return await engine.search_web(query, limit=limit if limit is not None else get_search_limit())

    @app.tool()
    async def browse_locate(url: str) -> dict[str, Any]:
        """Probe URL once, return detected mode + status."""
        return await engine.probe_one(url)

    @app.tool()
    async def browse_inspect(url: str) -> dict[str, Any]:
        """Peek URL: title + first ~500 chars. No DB write."""
        return await engine.inspect_one(url)

    @app.tool()
    async def browse_fetch(url: str) -> dict[str, Any]:
        """Fetch + index URL. Returns surgical receipt."""
        return await engine.fetch_one(url)

    @app.tool()
    async def browse_verify(url: str) -> dict[str, Any]:
        """Read one row from pages by URL."""
        return engine.verify_one(url)

    @app.tool()
    def browse_status() -> dict[str, Any]:
        """Engine health: pools, breaker, db."""
        return engine.engine_status()

    @app.tool()
    def browse_datetime() -> dict[str, Any]:
        """Current date, time, day-of-week, and timezone."""
        return engine.get_datetime()

    @app.tool()
    async def browse_extract(
        url: str,
        selector: str,
        mode: str = "css",
        output_type: str = "text",
        limit: Annotated[int, Field(default=10, ge=1, le=100, description="Max elements to extract")] = 10,
    ) -> dict[str, Any]:
        """Extract elements by CSS/XPath/text/regex selector."""
        return await engine.extract_from_url(url, selector, mode=mode, output_type=output_type, limit=limit)


# ── Query tier ─────────────────────────────────────────────────────

if _enabled("MCP_TIER_QUERY", "1"):

    @app.tool()
    def query_locate() -> dict[str, Any]:
        """List tables + row counts. Surgical."""
        return engine.query_locate()

    @app.tool()
    def query_search(
        query: str,
        table: str = "fts_pages",
        limit: Annotated[int, Field(default=10, ge=1, le=100, description="Max rows to return")] = 10,
    ) -> dict[str, Any]:
        """FTS5 search across pages/news/files. <=10 rows."""
        return engine.query_search(query, table=table, limit=limit)

    @app.tool()
    def query_select(
        sql: str,
        limit: Annotated[int, Field(default=20, ge=1, le=100, description="Max rows to return")] = 20,
    ) -> dict[str, Any]:
        """SELECT-only SQL (parameterless). Bounded."""
        return engine.query_select(sql, params=(), limit=limit)

    @app.tool()
    def query_export(table: str, out_path: str, fmt: str = "csv") -> dict[str, Any]:
        """Export table to CSV/JSON. Returns path only."""
        return engine.query_export(table, out_path, fmt=fmt)

    @app.tool()
    def query_stats() -> dict[str, Any]:
        """Per-table row counts + db_bytes."""
        return engine.query_stats()


# ── Crawl tier ─────────────────────────────────────────────────────

if _enabled("MCP_TIER_CRAWL", "0"):

    @app.tool()
    async def crawl_locate(url: str) -> dict[str, Any]:
        """Probe domain root, return mode + base path."""
        return await engine.crawl_locate(url)

    @app.tool()
    async def crawl_plan(
        url: str,
        max_links: Annotated[int, Field(default=25, ge=5, le=200, description="Max frontier links to enumerate")] = 25,
    ) -> dict[str, Any]:
        """Dry-run BFS: enumerate would-be frontier."""
        return await engine.crawl_plan(url, max_links=max_links)

    @app.tool()
    async def crawl_run(
        url: str,
        max_pages: Annotated[int, Field(default=50, ge=1, le=250, description="Max pages to crawl")] = 50,
        max_depth: Annotated[int, Field(default=3, ge=1, le=5, description="Max BFS depth")] = 3,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Bounded crawl. Indexes pages, returns receipt."""
        return await engine.crawl_run(
            url,
            max_pages=max_pages,
            max_depth=max_depth,
            run_id=run_id,
        )

    @app.tool()
    async def crawl_resume(
        run_id: str,
        url: str,
        max_pages: Annotated[int, Field(default=50, ge=1, le=250, description="Max additional pages to crawl")] = 50,
    ) -> dict[str, Any]:
        """Resume a crawl run from its checkpoint."""
        return await engine.crawl_resume(run_id, url, max_pages=max_pages)

    @app.tool()
    def crawl_verify(run_id: str) -> dict[str, Any]:
        """Run summary: pages, errors, dead-letter."""
        return engine.crawl_verify(run_id)

    @app.tool()
    async def browse_research(
        query: str,
        depth: Annotated[
            int | None,
            Field(
                default=None, ge=1, le=3, description="1=search only · 2=+fetch · 3=full (default: MCP_RESEARCH_DEPTH)"
            ),
        ] = None,
        fetch_top: Annotated[
            int | None,
            Field(
                default=None, ge=0, le=20, description="Top results to fetch, 0=all (default: MCP_RESEARCH_FETCH_TOP)"
            ),
        ] = None,
        limit: Annotated[int | None, Field(default=None, ge=1, le=50, description="Max search hits per query")] = None,
        breadth: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=3,
                description="1=single · 2=multi-angle · 3=wider (default: MCP_RESEARCH_BREADTH)",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Deep research: search, fetch+index, link-follow, FTS passages."""
        return await engine.research_topic(
            query,
            depth=depth if depth is not None else get_research_depth(),
            fetch_top=fetch_top if fetch_top is not None else get_research_fetch_top(),
            limit=limit,
            breadth=breadth if breadth is not None else get_research_breadth(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Web Browser MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("WEB_TRANSPORT", "stdio"),
    )
    args = parser.parse_args()

    if args.transport == "http":
        app.run(transport="streamable-http")
    else:
        app.run(transport="stdio")


if __name__ == "__main__":
    main()
