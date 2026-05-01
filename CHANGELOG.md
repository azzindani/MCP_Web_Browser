# Changelog

All notable changes to this project will be documented in this file.

---

## [0.1.0] — 2026-05-01

### Initial release

MCP Web Browser v0.1.0 is the first production-ready release of a local-first
self-hosted MCP server for end-to-end web access. It gives a language model
structured, surgical access to the web through 16 deterministic tools across
three tiers — without ever sending data to a cloud API.

| Tier | Tools | Purpose |
|---|---|---|
| `mcp_web_browser_basic` | 6 | Search, probe, fetch, verify web content |
| `mcp_web_browser_query` | 5 | Query and export the local SQLite knowledge base |
| `mcp_web_browser_crawl` | 5 | Domain crawl with checkpoint resume |

**Core capabilities:**
- Web search via SearXNG / DDG / Brave (no API key required)
- HTTP fetch with TLS fingerprint impersonation (`curl_cffi`)
- DOM/SPA rendering via Playwright stealth mode
- Domain crawl with circuit breaker, rate limiter, and checkpoint resume
- SQLite + FTS5 knowledge base for full-text query and export

**Engine architecture:**
- `engine/core/` — queue, router, scheduler, checkpoint, timer
- `engine/workers/` — http, browser, crawl, search, fingerprint, tls
- `engine/resilience/` — circuit_breaker, rate_limiter, retry
- `engine/db/` — schema, indexer, query (SQLite + FTS5, WAL mode)
- `engine/output/` — stream (JSONL), export (CSV/JSON), display

**Tests:** unit, integration, e2e, and smoke suites.
