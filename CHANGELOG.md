# Changelog

All notable changes to this project will be documented in this file.

---

## [0.1.1] — 2026-05-01

### New: `browse_extract` tool — structured element extraction (Basic tier)

Adds a CSS / XPath / text / regex extraction tool to the Basic tier. The model
can now extract specific elements from a fetched page without receiving the
entire DOM — keeping responses within the token budget.

**Tool:** `browse_extract` (PATCH role, Basic tier)

**New dev dependencies:** `lxml>=4.9`, `cssselect>=1.2`

### Standards alignment (mirrors `MCP_Data_Analyst`)

- **Python pinned to `==3.12.*`** (was `>=3.11`) for reproducible builds
- **Switched type checker from mypy to pyright** (`pyrightconfig.json` added; basic mode)
- **Ruff rules simplified** to `E, F, W, I, UP` with `E402/E501/F401/F841` ignores
- **Line length raised to 120** (from 100) — matches project standard
- **Dev deps moved to `[dependency-groups]`** (PEP 735 / uv native)
- **`[tool.uv] required-version = ">=0.5"`** added
- **`[tool.coverage.run]`** added

### CI improvements

- **Multi-OS matrix** — now tests ubuntu-22.04, macos-latest, windows-latest
- **Compliance gates use `shell: bash`** — run on all OSes (no Linux-only skip)
- **Added `engine/cli.py` exclusion** in stdout compliance gate
- **Full test suite (`tests/`)** run in both standard and constrained CI jobs
- **`tests/verify_tool_docstrings.py`** — rewritten: handles async tools, better decorator detection
- **Added `release.yml`** — tag-triggered GitHub release with auto-changelog

### Documentation and install

- **README fully rewritten** — mirrors Data_Analyst structure: platform support table,
  Windows-first PowerShell install, pre-install command, macOS/Linux bash alternative,
  usage examples, architecture tree, full CI sequence in Development section
- **`mcp.json` updated** — inline PowerShell self-updating launcher (clone-or-pull +
  sync + playwright install + run); bash alternative in README
- **`CHANGELOG.md`** added
- **`.gitattributes`** added — LF everywhere, CRLF for `.bat`
- **`.gitignore`** updated — adds `.mcp_versions/`, `*.mcp_state.json`, `*.mcp_receipt.json`

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
