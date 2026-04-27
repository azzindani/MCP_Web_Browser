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

---

## Configuration

All limits and storage paths flow through env vars. Defaults are tuned
for an 8 GB-VRAM / 9B-Q4 host.

| Variable               | Default                       | Purpose                              |
|------------------------|-------------------------------|--------------------------------------|
| `MCP_DATA_ROOT`        | current working directory     | Root for `*.db`, `*.jsonl`, exports  |
| `MCP_CONSTRAINED_MODE` | `0` (`1` in `mcp.json`)       | Shrinks every read/write cap         |
| `MCP_TIER_BASIC`       | `1`                           | Toggle the Basic tier (browse_*)     |
| `MCP_TIER_QUERY`       | `1`                           | Toggle the Query tier (query_*)      |
| `MCP_TIER_CRAWL`       | `0`                           | Toggle the Crawl tier (crawl_*)      |
| `MCP_SEARCH_BACKEND`   | `http://127.0.0.1:8888`       | SearXNG base URL (DDG/Brave fallback)|

### Constrained-mode caps

| Cap                       | Constrained | Default |
|---------------------------|-------------|---------|
| Rows per `query_*` call   | 20          | 100     |
| Search hits per call      | 10          | 50      |
| `crawl_run` max pages     | 25          | 250     |
| `crawl_run` max depth     | 3           | 5       |
| Inspect body chars        | 500         | 2 000   |

---

## Architecture

```
mcp_web_browser/
│
├── server.py            ← MCP entrypoint. THIN. One-liner tools only.
├── mcp.json             ← Self-updating launcher (clone+pull+sync)
├── pyproject.toml
│
├── engine/              ← Pure Python. ZERO `mcp.*` imports anywhere.
│   ├── __init__.py      ← Public entry points called by server.py
│   ├── core/            ← queue, router, scheduler, checkpoint, timer
│   ├── workers/         ← http, browser, crawl, search, fingerprint, tls
│   ├── resilience/      ← circuit_breaker, rate_limiter, retry
│   ├── db/              ← schema, indexer, query (SQLite + FTS5, WAL)
│   ├── output/          ← stream (JSONL), export (CSV/JSON), display
│   └── config/          ← defaults, per-domain overrides
│
├── shared/              ← Cross-cutting helpers
│   ├── platform_utils   ← is_constrained_mode(), get_max_rows()
│   ├── path_safety      ← resolve_path()
│   └── version_control  ← snapshot() / atomic_write_*
│
└── tests/   unit/  smoke/  integration/  e2e/
```

Tools are one-liners that delegate to engine functions; the engine
never imports `mcp.*`, so every module is unit-testable without the
SDK installed.

---

## Development

```sh
git clone https://github.com/azzindani/mcp_web_browser.git
cd mcp_web_browser
uv sync
uv run ruff check .
uv run mypy engine shared server.py
uv run pytest -q
```

The unit suite runs offline (mocked httpx, in-memory SQLite). One
integration test launches real Chromium when `MCP_BROWSER_TESTS=1`.

For the design and milestone tracker see [`PORT_PLAN.md`](PORT_PLAN.md)
and [`CLAUDE.md`](CLAUDE.md). The governing standard is
[`azzindani/Standards/local_mcp/STANDARDS.md`](https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md).

---

## Uninstall

```sh
rm -rf ~/.mcp/mcp_web_browser
# remove the mcp_web_browser entry from your host's mcp.json
```

---

## License

MIT
