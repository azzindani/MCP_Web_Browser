# Changelog

## v0.1.0 — 2026-05-02

First public release. A self-hosted MCP server that gives local LLMs end-to-end web access — no cloud APIs, no API keys.

---

### Features

#### Web Search
- Keyless search chain: SearXNG → DuckDuckGo → Bing → Brave → Playwright browser fallback (Google, then DDG)
- Playwright stealth browser fallback activates automatically when all HTTP backends fail or return 0 results
- Circuit breaker trips on captcha/bot-block to avoid hammering blocked endpoints
- Script-mismatch guard: rejects results in the wrong writing system (e.g. CJK results for a Latin query)
- Robust Bing parser with dual-strategy HTML extraction (handles both old and new Bing layouts)
- Current date injected into queries for time-sensitive searches

#### Deep Research (`browse_research`)
- Full pipeline: multi-angle search → parallel fetch + index → link following → FTS key-passage extraction
- `depth` levels: `1` = search only · `2` = search + parallel-fetch + index · `3` = full pipeline with link following and FTS enrichment
- `breadth` levels: `1` = single query · `2` = multi-angle (adds year + overview variants) · `3` = wider (adds best-practices + how-it-works angles)
- Domain deduplication in both search results and link following (max 2 per domain from search, max 1 per domain for links)
- Noise-domain filter strips social/video sites from link candidates
- Returns `sources[]`, `cite_hints`, `key_passages`, and a ready-to-paste `## Sources` block
- Unfetched sources and prescriptive `suggested_next` included in handover payload for multi-turn chaining

#### Fetch & Rendering
- `httpx` with HTTP/2 and TLS fingerprint impersonation via `curl_cffi`
- Playwright stealth browser for JavaScript-heavy / SPA pages (canvas, WebGL, battery spoofing)
- Block-page detection to avoid indexing error pages

#### Domain Crawl
- BFS crawl with configurable max pages and max depth
- Checkpoint-resume: interrupted crawls can be resumed from the last saved frontier
- File discovery: indexes PDFs, XLSX, CSV, and other linked documents

#### Knowledge Base
- SQLite + FTS5, WAL mode, `busy_timeout=5000`
- SHA-256 deduplication at index time
- Atomic JSONL/checkpoint writes via rename pattern
- `snapshot()` called before every persistent write

#### Tool Surface — 19 tools across 3 tiers

| Tier | Tools | Default |
|---|---|---|
| Basic | `browse_search`, `browse_locate`, `browse_inspect`, `browse_fetch`, `browse_extract`, `browse_verify`, `browse_status`, `browse_datetime` | On |
| Query | `query_locate`, `query_search`, `query_select`, `query_export`, `query_stats` | On |
| Crawl | `crawl_locate`, `crawl_plan`, `crawl_run`, `crawl_resume`, `crawl_verify`, `browse_research` | Off |

#### Configuration
- All caps and defaults controlled by environment variables — no hardcoded limits
- `MCP_CONSTRAINED_MODE=1` halves all caps for lower-memory machines
- Tier toggles (`MCP_TIER_BASIC`, `MCP_TIER_QUERY`, `MCP_TIER_CRAWL`) allow staying within model context budgets
- Research defaults tunable without code changes:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_SEARCH_LIMIT` | `10` | Default hit count for `browse_search` |
| `MCP_RESEARCH_DEPTH` | `2` | Default depth for `browse_research` |
| `MCP_RESEARCH_FETCH_TOP` | `5` | Default fetch_top for `browse_research` |
| `MCP_RESEARCH_BREADTH` | `1` | Default breadth for `browse_research` |

#### Resilience
- Per-domain circuit breaker (open on repeated failures, resets after cooldown)
- Token-bucket rate limiter per domain
- Retry with exponential back-off
- `MCP_CONSTRAINED_MODE` read at call time — tests can flip it without reloading modules

---

### Platform Support

| Platform | Status |
|---|---|
| Windows 11 | Tested — real-world verified |
| macOS | CI passes (untested by hand) |
| Linux | CI passes (untested by hand) |

---

### Known Limitations

- All three tiers enabled simultaneously may exceed a model's tool-schema context budget; enable at most two at a time on constrained hosts
- Browser fallback (Google/DDG via Playwright) requires `playwright install chromium chromium-headless-shell` to be run once before first use
- Deep research at `depth=3` with `breadth=3` can take 30–60 seconds depending on network conditions and backend availability
