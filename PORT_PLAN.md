# PORT_PLAN.md — Krawl → `mcp_web_browser`

> Port the [Krawl](https://github.com/azzindani/krawl) TypeScript engine into a
> self-hosted Python MCP server (`mcp_web_browser`) that exposes web-fetching
> and crawling capability to a local LLM under just-in-time (JIT) execution.

This plan is bound by the rules in
[`azzindani/Standards/local_mcp/STANDARDS.md`](https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md).
Any deviation must be justified inline.

---

## 1. Mission

```
End-to-end web access for a local LLM in one MCP server:

  web search  →  URL probe  →  HTTP / API fetch  →  DOM / SPA render
              →  domain crawl  →  SQLite knowledge base  →  FTS query

The model decides what to look up and when. The MCP server runs each
short bounded step synchronously, returns a surgical confirmation, and
persists every result to a local SQLite file the model can query later.
Zero LLM in the engine. Zero cloud APIs.
```

## 2. Non-Negotiable Constraints

> **Constraint 1 (hardware):** must run on 8 GB GPU / 9B Q4 model with
> ~10–12k effective context. Tool schemas total ≤ 700 tokens, read
> responses ≤ 500 tokens, write confirmations ≤ 150 tokens.

> **Constraint 2 (sovereignty):** no cloud APIs, no API keys, no third
> party services. Playwright runs locally. SQLite is the only store.

> **Constraint 3 (just-in-time):** every tool call is one short, bounded
> operation. The model orchestrates multi-step workflows by calling
> small tools in sequence — the engine never auto-loops behind the model's
> back.

---

## 3. Source → Target Module Map

```
krawl (TypeScript / Node 20)              mcp_web_browser (Python 3.11+)
─────────────────────────────────────     ─────────────────────────────────
krawl.ts                              →   engine/cli.py
core/queue.ts                         →   engine/core/queue.py
core/router.ts                        →   engine/core/router.py
core/scheduler.ts                     →   engine/core/scheduler.py
core/checkpoint.ts                    →   engine/core/checkpoint.py
core/timer.ts                         →   engine/core/timer.py
workers/http.ts                       →   engine/workers/http_worker.py
workers/browser.ts                    →   engine/workers/browser_worker.py
workers/crawl.ts                      →   engine/workers/crawl_worker.py
(new — not in krawl)                  →   engine/workers/search_worker.py
workers/fingerprint.ts                →   engine/workers/fingerprint.py
workers/tls.ts                        →   engine/workers/tls.py
resilience/circuit_breaker.ts         →   engine/resilience/circuit_breaker.py
resilience/rate_limiter.ts            →   engine/resilience/rate_limiter.py
resilience/retry.ts                   →   engine/resilience/retry.py
db/schema.ts                          →   engine/db/schema.py
db/indexer.ts                         →   engine/db/indexer.py
db/query.ts                           →   engine/db/query.py
output/stream.ts                      →   engine/output/stream.py
output/export.ts                      →   engine/output/export.py
output/display.ts                     →   engine/output/display.py
config/defaults.ts                    →   engine/config/defaults.py
config/domains.ts                     →   engine/config/domains.py
selectors/                            →   engine/selectors/
tasks/*.json                          →   tasks/*.json   (unchanged format)
```

## 4. Dependency Mapping

| TypeScript (krawl)         | Python (mcp_web_browser)         | Notes                                 |
|----------------------------|----------------------------------|---------------------------------------|
| `playwright`               | `playwright` (sync API)          | Same browser, same stealth surface    |
| `better-sqlite3`           | `sqlite3` (stdlib) + WAL pragma  | No third-party DB driver              |
| `undici` / `fetch`         | `httpx[http2]`                   | TLS impersonation via `curl_cffi`     |
| `zod`                      | `pydantic` v2                    | Tool schemas + task validation        |
| `p-limit`                  | `asyncio.Semaphore`              | Per-pool concurrency                  |
| `pdf-parse`                | `pypdf`                          | Extract text only, never embed bytes  |
| MCP wrapper (n/a)          | `mcp` (Python SDK, stdio)        | Server layer, kept thin               |

Total runtime deps: **6** (cap = 7 per `dependency policy`).
No deps with native build steps beyond Playwright's bundled Chromium.

### 4.1 Web search backends (no API keys)

`engine/workers/search_worker.py` runs entirely on the existing HTTP /
browser workers — **no new dependency**. Backends are tried in order
until one returns results:

| Order | Backend                    | Transport     | Auth   |
|-------|----------------------------|---------------|--------|
| 1     | Self-hosted SearXNG        | `httpx` JSON  | none   |
| 2     | DuckDuckGo HTML endpoint   | `httpx` HTML  | none   |
| 3     | Brave Search HTML endpoint | `httpx` HTML  | none   |
| 4     | Browser-rendered fallback  | Playwright    | none   |

Backend URL is taken from `MCP_SEARCH_BACKEND` (default
`http://127.0.0.1:8888` for SearXNG). All requests pass through the
same circuit-breaker / rate-limiter / retry layer as any other URL.

## 5. Repository Layout

```
mcp_web_browser/
│
├── server.py                  ← MCP entrypoint. Tool funcs are one-liners.
├── mcp.json                   ← self-updating launch entry (clone+uv sync)
├── pyproject.toml
├── uv.lock
├── README.md
├── CLAUDE.md
├── PORT_PLAN.md               ← this file
│
├── engine/                    ← zero MCP imports anywhere under here
│   ├── __init__.py
│   ├── cli.py                 ← optional standalone CLI (no MCP)
│   ├── core/
│   │   ├── queue.py
│   │   ├── router.py
│   │   ├── scheduler.py
│   │   ├── checkpoint.py
│   │   └── timer.py
│   ├── workers/
│   │   ├── http_worker.py
│   │   ├── browser_worker.py
│   │   ├── crawl_worker.py
│   │   ├── search_worker.py    ← web search (SearXNG / DDG / Brave)
│   │   ├── fingerprint.py
│   │   └── tls.py
│   ├── resilience/
│   │   ├── circuit_breaker.py
│   │   ├── rate_limiter.py
│   │   └── retry.py
│   ├── db/
│   │   ├── schema.py
│   │   ├── indexer.py
│   │   └── query.py
│   ├── output/
│   │   ├── stream.py
│   │   ├── export.py
│   │   └── display.py
│   ├── config/
│   │   ├── defaults.py
│   │   └── domains.py
│   └── selectors/
│
├── shared/
│   ├── __init__.py
│   ├── platform_utils.py      ← is_constrained_mode(), get_max_rows()
│   ├── path_safety.py         ← resolve_path()
│   └── version_control.py     ← snapshot() / atomic write
│
└── tests/
    ├── unit/
    ├── smoke/
    ├── integration/
    └── e2e/
```

## 6. Just-In-Time Execution Model

End-to-end browsing is composed by the model from short tool calls.
Typical flow (each line is one MCP round-trip):

```
LLM turn                       MCP server                      Engine
────────                       ──────────                      ──────
call browse_search(q)    ─►    validate args (pydantic)
                               engine.search_web(q)     ─►    SearchWorker.run()
                                                              (SearXNG / DDG / Brave)
                          ◄── [≤10 hits: title, url, snippet]

call browse_locate(url)  ─►    engine.probe(url)        ─►    Router.resolve(url)
                          ◄── { mode, cached, domain_stats }

call browse_inspect(url) ─►    engine.inspect(url)      ─►    HttpWorker.peek()
                          ◄── { title, status, head: ≤500 chars }

call browse_fetch(url)   ─►    engine.fetch_one(url)    ─►    Router → Worker.run()
                                                              Indexer.index(result)
                                                              Stream.append(result)
                          ◄── { ok, url, mode, rows, ms }

call crawl_run(domain)   ─►    engine.crawl(domain)     ─►    CrawlWorker (BFS)
                                                              checkpoint per N tasks
                          ◄── { ok, run_id, pages, errors }

call query_search(q)     ─►    engine.fts_search(q)     ─►    QueryEngine.search()
                          ◄── [≤10 rows from fts_pages, surgical]
```

The model can stop at any step — it does not have to run the whole
chain. Each tool returns a structured receipt, never raw HTML.

Key rules:

- **No background loops in MCP tools.** Each call returns within the
  per-domain timeout budget. Long crawls are explicitly initiated via
  `crawl_run` and tracked through `crawl_resume`, never kept alive
  across calls.
- **Snapshot before every persistent write** (SQLite WAL + atomic rename
  for JSONL/checkpoint files).
- **Surgical reads only.** Tool responses include `truncated: bool` and
  `total: int` whenever the underlying result set exceeds the cap.

## 7. MCP Tool Design (Three-Tier Split, ≤ 8 Tools per Tier)

Every tier follows the **LOCATE → INSPECT → PATCH → VERIFY** pattern.

### 7.1 Tier — `mcp_web_browser_basic` (search + single URL ops, default-on)

| # | Tool              | Pattern role | Purpose                                                  |
|---|-------------------|--------------|----------------------------------------------------------|
| 1 | `browse_search`   | LOCATE       | Web search (SearXNG/DDG/Brave) → ≤10 result hits         |
| 2 | `browse_locate`   | LOCATE       | Probe URL, return detected mode + cached domain stats    |
| 3 | `browse_inspect`  | INSPECT      | Fetch URL once, return title + first N chars + status    |
| 4 | `browse_fetch`    | PATCH        | Execute fetch (HTTP/API/DOM/SPA via router), index it    |
| 5 | `browse_verify`   | VERIFY       | Read one row from `pages` by URL, return summary         |
| 6 | `browse_status`   | (aux)        | Return engine health: pools idle, breaker open count     |

`browse_search` is the open-web entry point. `query_search` (query
tier) is its local-DB counterpart — the model picks one based on
whether the data has been ingested already.

### 7.2 Tier — `mcp_web_browser_query` (read-only data access)

| # | Tool                 | Pattern role | Purpose                                              |
|---|----------------------|--------------|------------------------------------------------------|
| 1 | `query_locate`       | LOCATE       | List tables + row counts                             |
| 2 | `query_search`       | INSPECT      | Bounded FTS5 query, ≤10 rows                         |
| 3 | `query_select`       | INSPECT      | Whitelisted SELECT (param binding only, no DDL)      |
| 4 | `query_export`       | PATCH        | Write CSV/JSON file, return path only                |
| 5 | `query_stats`        | VERIFY       | Per-table counts + last-updated timestamps           |

### 7.3 Tier — `mcp_web_browser_crawl` (multi-URL traversal)

| # | Tool                 | Pattern role | Purpose                                                |
|---|----------------------|--------------|--------------------------------------------------------|
| 1 | `crawl_locate`       | LOCATE       | Probe domain root, return mode + estimated frontier    |
| 2 | `crawl_plan`         | INSPECT      | Dry-run: enumerate first N links at depth ≤ 1          |
| 3 | `crawl_run`          | PATCH        | Execute bounded crawl (`max_pages`, `max_depth`)       |
| 4 | `crawl_resume`       | PATCH        | Resume from checkpoint by `run_id`                     |
| 5 | `crawl_verify`       | VERIFY       | Return run summary: pages, errors, dead-letter count   |

Total simultaneously loadable: **16** tools across 3 tiers (basic = 6,
query = 5, crawl = 5). Each tier is still ≤ 8. The 12-tool ceiling is
respected as long as at most two tiers are enabled together; basic +
query (the default pair) is exactly 11.

## 8. Engine / Server Separation

```
engine/**           server.py
─────────           ─────────
no MCP imports      from mcp.server import Server
pure Python         from engine import fetch_one, search, ...
sync or async       @app.tool() one-liners
unit-testable       def browse_fetch(url: str) -> dict:
without MCP             return fetch_one(url)
```

Lint check (CI):

```
grep -rE "^(from|import)\s+mcp" engine/ && exit 1 || exit 0
```

## 9. Token Budget Discipline

Reads `MCP_CONSTRAINED_MODE` **at call time**, not import time.

| Limit                     | Constrained (≤8 GB VRAM) | Default |
|---------------------------|--------------------------|---------|
| Rows per `query_*` call   | 20                       | 100     |
| Search hits per call      | 10                       | 50      |
| `crawl_run` max pages     | 25                       | 250     |
| `crawl_run` max depth     | 3                        | 5       |
| Inspect body chars        | 500                      | 2 000   |

Tool docstring length: ≤ 80 chars. Enforced by:

```python
assert all(len((t.__doc__ or "")) <= 80 for t in TOOLS)
```

## 10. Snapshot & State Protocol

```
SQLite              WAL mode, busy_timeout=5000, checkpoint per run end
JSONL stream        append-only, fsync on rotation, atomic rename on close
Checkpoint file     write to .tmp → fsync → rename → fsync(dir)
Router cache        same atomic-rename pattern
```

Every tool that mutates state calls `shared.version_control.snapshot()`
before writing. Failure to snapshot is a hard error; the tool returns
`{ ok: false, error: "snapshot_failed" }` instead of attempting the write.

## 11. Path & Process Safety

- All user-supplied paths pass through `shared.path_safety.resolve_path()`.
- `subprocess.run(..., shell=False)`, argument list only. No `eval`,
  no `exec`, no template-string SQL.
- Playwright launches Chromium with `--no-sandbox` only when running as
  non-root in a container; otherwise sandbox stays on.

## 12. Distribution — Self-Updating `mcp.json`

```jsonc
{
  "mcpServers": {
    "mcp_web_browser": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "${HOME}/.mcp/mcp_web_browser",
        "python", "-m", "mcp_web_browser.server"
      ],
      "env": { "MCP_CONSTRAINED_MODE": "auto" }
    }
  }
}
```

First launch clones the repo into `~/.mcp/mcp_web_browser`, runs
`uv sync`, then starts the server. Subsequent launches `git pull --ff-only`
and re-sync. No separate install script.

## 13. Absolute Prohibitions (carried verbatim from STANDARDS §38)

- Never print to stdout from inside the server (corrupts MCP stdio).
- Never return plain strings from tools — always structured dicts.
- Never write to disk without `snapshot()` first.
- Never use `eval`/`exec`/`shell=True` on any input.
- Never exceed 8 tools in a single tier or 12 simultaneously loaded.
- Never call cloud APIs or require API keys.
- Never embed an LLM call inside the engine.
- Never hardcode token / row limits — always go through
  `shared.platform_utils`.
- Never reach back into `server.py` from inside `engine/**`.

## 14. Milestones

| ID  | Deliverable                                              | Exit criterion                                    |
|-----|----------------------------------------------------------|---------------------------------------------------|
| M1  | Repo scaffold, `pyproject.toml`, lint + mypy + pytest     | `uv sync && pytest -q` passes on empty tests      |
| M2  | `engine/db/` schema + indexer + query                    | Can write & query 1k pages locally                |
| M3  | `engine/workers/http_worker.py` + resilience             | Yahoo Finance JSON endpoint passes integration    |
| M3b | `engine/workers/search_worker.py` (SearXNG / DDG / Brave) | `browse_search("hello")` returns ≥ 1 hit offline-OK |
| M4  | `engine/workers/browser_worker.py` (Playwright stealth)  | Headless extract on SPA fixture                   |
| M5  | `engine/workers/crawl_worker.py` + checkpoint            | Domain sweep on local fixture, resumes after kill |
| M6  | `server.py` + Basic tier (6 tools incl. `browse_search`) | LM Studio loads tools, all four roles green       |
| M7  | Query tier + Crawl tier                                  | All 16 tools schema-validate < 700 tokens / tier  |
| M8  | `mcp.json` self-update + README + CI                     | `git clone` + first launch works on a clean box   |

## 15. Out of Scope

- Backwards compatibility with `krawl.ts` CLI flags — Python CLI is
  optional and minimal.
- Cluster / distributed crawl coordination. Parallel instances stay
  fully independent (one SQLite per instance), as in krawl.
- Any LLM call inside the engine. Intelligence stays on the model side.
- Cookies / login flows beyond per-task header injection.
- Paid search APIs (Google CSE, Bing API, Serper, etc.). All search
  backends must be keyless and self-hostable per `Constraint 2`.

## 16. References

- Source engine: <https://github.com/azzindani/krawl>
- Krawl architecture: `docs/ARCHITECTURE.md` in the krawl repo
- Standard: <https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md>
