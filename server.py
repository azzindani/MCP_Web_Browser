"""MCP server entrypoint. THIN. One-liner tools only.

Tier: `mcp_web_browser_basic` (LOCATE → INSPECT → PATCH → VERIFY + aux).
Six tools, each docstring ≤ 80 chars; engine logic lives in engine/**.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import engine

app: FastMCP = FastMCP("mcp_web_browser")


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


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
