# MCP Web Browser

A self-hosted MCP server that gives local LLMs end-to-end web access. No cloud APIs, no API keys — everything runs on your machine.

## Features

- **16 tools** across 3 tiers: basic (6), query (5), crawl (5)
- **LOCATE → INSPECT → PATCH → VERIFY** workflow for bounded, surgical web access
- **Web search** — keyless: SearXNG → DuckDuckGo HTML → Brave HTML fallback chain
- **HTTP / API fetch** — `httpx` + HTTP/2, TLS fingerprint impersonation via `curl_cffi`
- **DOM / SPA rendering** — Playwright stealth (canvas, WebGL, battery spoofing)
- **Domain crawl** — BFS, file discovery (PDF/XLSX/CSV/...), checkpoint resume
- **SQLite + FTS5 knowledge base** — WAL mode, atomic indexer, SHA-256 dedup
- **Resilience** — per-domain circuit breaker + token bucket rate limiter + retry
- **Constrained mode** — reduces caps for lower-memory / smaller-context machines
- **No cloud** — no API keys, no third-party services in the engine path

## Quick Install (LM Studio)

> **Tested on Windows 11** with LM Studio 0.4.x and uv 0.5+.

### Requirements

- **Git** — `git --version`
- **Python 3.12** — `python --version`
- **uv** — `uv --version` ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **LM Studio** with a model that supports tool calling (Gemma 4, Qwen 3.5, etc.)
- ~2 GB free disk for Playwright Chromium

### Platform Support

| Platform | Status |
|---|---|
| Windows | Tested — real-world verified (Windows 11) |
| macOS | Untested — CI/CD pipeline passes |
| Linux | Untested — CI/CD pipeline passes |

> Real-world usage has only been verified on Windows. macOS and Linux are supported by design and pass the automated CI pipeline, but have not been tested by hand.

### First Run

The first launch clones the repo and installs dependencies (~2-5 minutes, including Playwright Chromium). Subsequent launches are instant.

> **Pre-install recommended:** To avoid the 60-second LM Studio connection timeout on first launch, run this once in PowerShell before connecting:
> ```powershell
> $d = Join-Path $env:USERPROFILE '.mcp_servers\MCP_Web_Browser'
> $g = Join-Path $d '.git'
> if (!(Test-Path $g)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d }; git clone https://github.com/azzindani/MCP_Web_Browser.git $d --quiet }
> Set-Location $d; uv sync; uv run playwright install chromium
> ```
> If you skip this step and LM Studio times out, press **Restart** in the MCP Servers panel.

### Steps

1. Open LM Studio → **Developer** tab (`</>` icon) or find via **Integrations**
2. Find **mcp.json** or **Edit mcp.json** → click to open
3. Paste this config:

```json
{
  "mcpServers": {
    "mcp_web_browser": {
      "command": "powershell",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$d = Join-Path $env:USERPROFILE '.mcp_servers\\MCP_Web_Browser'; $g = Join-Path $d '.git'; if (!(Test-Path $g)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d }; git clone https://github.com/azzindani/MCP_Web_Browser.git $d --quiet } else { Set-Location $d; git fetch origin --quiet; git reset --hard FETCH_HEAD --quiet }; Set-Location $d; uv sync --quiet; uv run playwright install chromium --quiet; uv run python server.py"
      ],
      "env": {
        "MCP_CONSTRAINED_MODE": "0",
        "MCP_TIER_BASIC": "1",
        "MCP_TIER_QUERY": "1",
        "MCP_TIER_CRAWL": "0",
        "MCP_SEARCH_BACKEND": "http://127.0.0.1:8888"
      },
      "timeout": 600000
    }
  }
}
```

4. Wait for the blue dot next to **mcp_web_browser**
5. Start chatting — the model will see all 11 default tools (Basic + Query)

### macOS / Linux

Replace the `"command"` and `"args"` with the bash equivalent:

```json
{
  "mcpServers": {
    "mcp_web_browser": {
      "command": "bash",
      "args": [
        "-c",
        "d=\"$HOME/.mcp_servers/MCP_Web_Browser\"; if [ ! -d \"$d/.git\" ]; then rm -rf \"$d\"; git clone https://github.com/azzindani/MCP_Web_Browser.git \"$d\" --quiet; else cd \"$d\" && git fetch origin --quiet && git reset --hard FETCH_HEAD --quiet; fi; cd \"$d\"; uv sync --quiet; uv run playwright install chromium --quiet; uv run python server.py"
      ],
      "env": {
        "MCP_CONSTRAINED_MODE": "0",
        "MCP_TIER_BASIC": "1",
        "MCP_TIER_QUERY": "1",
        "MCP_TIER_CRAWL": "0",
        "MCP_SEARCH_BACKEND": "http://127.0.0.1:8888"
      },
      "timeout": 600000
    }
  }
}
```

---

## Available Tools

Tiers are toggled by environment variable. Default-on: Basic + Query (11 tools, fits the 12-tool simultaneous ceiling). Crawl is off by default — enable it via `MCP_TIER_CRAWL=1` and disable Query if you need to stay under the cap.

### Basic tier — `MCP_TIER_BASIC=1` (6 tools, default on)

| Tool | Purpose |
|---|---|
| `browse_search` | Web search (SearXNG / DDG / Brave). Returns up to 10 hits. |
| `browse_locate` | Probe a URL once, return detected mode + HTTP status. |
| `browse_inspect` | Peek a URL: title + first ~500 chars. No DB write. |
| `browse_fetch` | Fetch + index a URL. Returns surgical receipt. |
| `browse_verify` | Read one row from `pages` by URL. |
| `browse_status` | Engine health: pools, circuit breaker, db stats. |

### Query tier — `MCP_TIER_QUERY=1` (5 tools, default on)

| Tool | Purpose |
|---|---|
| `query_locate` | List tables + row counts in the knowledge base. |
| `query_search` | FTS5 full-text search across pages / news / files. |
| `query_select` | SELECT-only SQL (parameterised). Bounded to max rows. |
| `query_export` | Export table to CSV or JSON. Returns path only. |
| `query_stats` | Per-table row counts + db file size in bytes. |

### Crawl tier — `MCP_TIER_CRAWL=1` (5 tools, off by default)

| Tool | Purpose |
|---|---|
| `crawl_locate` | Probe domain root, return detected mode + base path. |
| `crawl_plan` | Dry-run BFS: enumerate the would-be frontier. |
| `crawl_run` | Bounded crawl. Indexes pages, returns receipt. |
| `crawl_resume` | Resume a crawl run from its last checkpoint. |
| `crawl_verify` | Run summary: pages indexed, errors, dead-letter count. |

---

## Configuration

All limits and storage paths flow through environment variables. Defaults are tuned for an 8 GB-VRAM / 9B-Q4 host.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_DATA_ROOT` | current working directory | Root for `*.db`, `*.jsonl`, exports |
| `MCP_CONSTRAINED_MODE` | `0` | Set to `1` for low-memory machines |
| `MCP_TIER_BASIC` | `1` | Toggle the Basic tier (`browse_*`) |
| `MCP_TIER_QUERY` | `1` | Toggle the Query tier (`query_*`) |
| `MCP_TIER_CRAWL` | `0` | Toggle the Crawl tier (`crawl_*`) |
| `MCP_SEARCH_BACKEND` | `http://127.0.0.1:8888` | SearXNG base URL (DDG/Brave fallback) |

### Constrained-mode caps

| Cap | Constrained | Default |
|---|---|---|
| Rows per `query_*` call | 20 | 100 |
| Search hits per call | 10 | 50 |
| `crawl_run` max pages | 25 | 250 |
| `crawl_run` max depth | 3 | 5 |
| Inspect body chars | 500 | 2 000 |

---

## Usage Examples

### Search the web

```
Search for recent papers on RAG retrieval techniques
```

### Fetch and index a page

```
Fetch https://example.com/article and save it to the knowledge base
```

### Query the knowledge base

```
Search the knowledge base for mentions of "vector database"
```

### Crawl a domain

```
Crawl https://docs.example.com up to depth 3 and index all pages
```

### Resume an interrupted crawl

```
Resume the last crawl — it was interrupted at page 47
```

---

## Architecture

```
MCP_Web_Browser/
├── server.py            ← MCP entrypoint. THIN. One-liner tools only.
├── mcp.json             ← Self-updating launcher (PowerShell / bash)
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
├── shared/              ← Cross-cutting helpers (no MCP imports)
│   ├── platform_utils.py   ← is_constrained_mode(), get_max_rows()
│   ├── path_safety.py      ← resolve_path()
│   └── version_control.py  ← snapshot() / atomic_write_*
│
└── tests/
    ├── conftest.py
    ├── unit/            ← 17 files; import engine directly, no MCP running
    ├── integration/     ← hits real network (opt-in via MCP_BROWSER_TESTS=1)
    ├── e2e/             ← full pipeline tests
    └── smoke/           ← server round-trip tests
```

Tools are one-liners that delegate to engine functions. The engine never imports `mcp.*`, so every module is unit-testable without the SDK installed.

---

## Development

```bash
# Clone and install
git clone https://github.com/azzindani/MCP_Web_Browser.git
cd MCP_Web_Browser
uv sync

# Install Playwright Chromium (required for browser_worker tests)
uv run playwright install chromium

# Full CI sequence — run in order before every commit
uv run ruff format engine/ shared/ server.py tests/
uv run ruff check engine/ shared/ server.py tests/
uv run pyright engine/ shared/ server.py
uv run python tests/verify_tool_docstrings.py
uv run pytest tests/unit -q --tb=short

# Run in constrained mode
MCP_CONSTRAINED_MODE=1 uv run pytest tests/unit -q --tb=short
```

The unit suite runs offline (mocked httpx, in-memory SQLite). Integration and e2e tests hit the real network and require `MCP_BROWSER_TESTS=1`.

---

## Uninstall

**Step 1:** Remove from LM Studio
1. Open LM Studio → Developer tab (`</>`)
2. Delete the `mcp_web_browser` entry from MCP Servers
3. Restart LM Studio

**Step 2:** Delete installed files

```powershell
# Windows
Remove-Item -Recurse -Force "$env:USERPROFILE\.mcp_servers\MCP_Web_Browser"
```

```bash
# macOS / Linux
rm -rf "$HOME/.mcp_servers/MCP_Web_Browser"
```

---

## License

MIT
