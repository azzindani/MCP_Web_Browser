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

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

import engine

app: FastMCP = FastMCP("mcp_web_browser")


def _enabled(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


# ── Basic tier ─────────────────────────────────────────────────────

if _enabled("MCP_TIER_BASIC", "1"):

    @app.tool()
    async def browse_search(query: str, limit: int | None = None) -> dict[str, Any]:
        """Web search (SearXNG/DDG/Brave). Returns <=10 hits."""
        return await engine.search_web(query, limit=limit)

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
        limit: int | None = None,
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
    def query_search(query: str, table: str = "fts_pages", limit: int | None = None) -> dict[str, Any]:
        """FTS5 search across pages/news/files. <=10 rows."""
        return engine.query_search(query, table=table, limit=limit)

    @app.tool()
    def query_select(sql: str, limit: int | None = None) -> dict[str, Any]:
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
    async def crawl_plan(url: str, max_links: int = 25) -> dict[str, Any]:
        """Dry-run BFS: enumerate would-be frontier."""
        return await engine.crawl_plan(url, max_links=max_links)

    @app.tool()
    async def crawl_run(
        url: str,
        max_pages: int | None = None,
        max_depth: int | None = None,
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
    async def crawl_resume(run_id: str, url: str, max_pages: int | None = None) -> dict[str, Any]:
        """Resume a crawl run from its checkpoint."""
        return await engine.crawl_resume(run_id, url, max_pages=max_pages)

    @app.tool()
    def crawl_verify(run_id: str) -> dict[str, Any]:
        """Run summary: pages, errors, dead-letter."""
        return engine.crawl_verify(run_id)

    @app.tool()
    async def browse_research(
        query: str,
        depth: int = 2,
        fetch_top: int = 3,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search + auto-fetch top results. Returns sources + cite_hints."""
        return await engine.research_topic(query, depth=depth, fetch_top=fetch_top, limit=limit)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
