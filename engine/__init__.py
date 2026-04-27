"""Pure-Python engine. No imports from `mcp.*` are allowed under this package.

The thin server (server.py) imports `engine.fetch_one`, `engine.search_web`,
etc., and exposes them as MCP tool one-liners. Everything below is unit-
testable without the MCP SDK installed.
"""
