# MCP Web Browser

> Self-hosted MCP server giving a local LLM end-to-end web access:
> **search → probe → fetch → render → crawl → query** — all on your
> hardware, no cloud APIs, no API keys.

A Python port of the [krawl](https://github.com/azzindani/krawl) engine
wrapped as a [Model Context Protocol](https://modelcontextprotocol.io/)
server. Tools call the engine just-in-time; every response is bounded
to fit a 9B-Q4 model's context budget (≤ 700 tokens of schema, ≤ 500
tokens per read response).

---

## Features

- 🔎 **Web search** — keyless: SearXNG → DuckDuckGo HTML → Brave HTML
- 🌐 **HTTP / API fetch** — `httpx` + HTTP/2, Yahoo chart auto-parser
- 🤖 **DOM / SPA rendering** — Playwright stealth (canvas, WebGL, battery)
- 🕸️ **Domain crawl** — BFS, file discovery (PDF/XLSX/CSV/…), checkpointed
- 🗃️ **SQLite + FTS5** — WAL mode, atomic indexer, SHA-256 dedup
- 🛡️ **Resilience** — per-domain circuit breaker + token bucket + retry
- 📐 **Bounded tool surface** — 16 tools across 3 env-toggled tiers
- 🧰 **No cloud** — no API keys, no third-party services in the engine

---

## Quick install (LM Studio)

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`pipx install uv` or `brew install uv`)
- ~2 GB free disk for Playwright Chromium
- Optional: a self-hosted SearXNG instance (defaults to `http://127.0.0.1:8888`)

### One-time setup

```sh
# 1. Drop mcp.json into LM Studio's MCP config directory
#    (LM Studio → Settings → MCP Servers → Edit JSON)
curl -fsSL \
  https://raw.githubusercontent.com/azzindani/mcp_web_browser/main/mcp.json \
  -o ~/.lmstudio/mcp.json

# 2. First launch from LM Studio runs the preLaunch script:
#    git clone, uv sync, playwright install chromium.
```

The same `mcp.json` works in any MCP-compatible host (Claude Desktop,
Cursor, Continue) — just point the host at it.

---

## Available tools

Tiers are toggled by environment variable. Default-on: Basic + Query
(11 tools, fits the 12-tool simultaneous ceiling). Crawl is off by
default; enable it via `MCP_TIER_CRAWL=1` and disable Query if you need
to stay under the cap.

### Basic tier — `MCP_TIER_BASIC=1` (6 tools, default)

| Tool             | Role     | Purpose                                                |
|------------------|----------|--------------------------------------------------------|
| `browse_search`  | LOCATE   | Web search (SearXNG / DDG / Brave). ≤10 hits.          |
| `browse_locate`  | LOCATE   | Probe URL once, return detected mode + status.         |
| `browse_inspect` | INSPECT  | Peek URL: title + first ~500 chars. No DB write.       |
| `browse_fetch`   | PATCH    | Fetch + index URL. Returns surgical receipt.           |
| `browse_verify`  | VERIFY   | Read one row from `pages` by URL.                      |
| `browse_status`  | aux      | Engine health: pools, breaker, db.                     |

### Query tier — `MCP_TIER_QUERY=1` (5 tools, default)

| Tool             | Role     | Purpose                                                |
|------------------|----------|--------------------------------------------------------|
| `query_locate`   | LOCATE   | List tables + row counts.                              |
| `query_search`   | INSPECT  | FTS5 search across pages / news / files. ≤10 rows.     |
| `query_select`   | INSPECT  | SELECT-only SQL (parameter-less). Bounded.             |
| `query_export`   | PATCH    | Export table → CSV/JSON. Returns path only.            |
| `query_stats`    | VERIFY   | Per-table row counts + db_bytes.                       |

### Crawl tier — `MCP_TIER_CRAWL=1` (5 tools, off by default)

| Tool             | Role     | Purpose                                                |
|------------------|----------|--------------------------------------------------------|
| `crawl_locate`   | LOCATE   | Probe domain root, return mode + base path.            |
| `crawl_plan`     | INSPECT  | Dry-run BFS: enumerate would-be frontier.              |
| `crawl_run`      | PATCH    | Bounded crawl. Indexes pages, returns receipt.         |
| `crawl_resume`   | PATCH    | Resume a crawl run from its checkpoint.                |
| `crawl_verify`   | VERIFY   | Run summary: pages, errors, dead-letter.               |
