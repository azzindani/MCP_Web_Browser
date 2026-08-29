# CLAUDE.md — `mcp_web_browser`

> Operating manual for any AI coding agent (Claude Code, Cursor, Codex,
> etc.) working in this repository. Read this before touching any file.

This project is governed by
[`azzindani/Standards/local_mcp/STANDARDS.md`](https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md).
This file is the project-level extension of that standard. Where they
disagree, the standard wins.

---

## 1. Project Overview

`mcp_web_browser` is a **self-hosted MCP server** that gives a local LLM
end-to-end web access in one place: web search, URL probe, HTTP/API
fetch, DOM/SPA rendering, domain crawl, and full-text query — all
backed by a local SQLite knowledge base.

```
Pipeline: web search → URL probe → HTTP/API fetch → DOM/SPA render
          → domain crawl → SQLite + FTS5 → query / export

Goal:   port the krawl engine (TypeScript) into Python and expose it
        as an MCP server for just-in-time tool calls from a local model.

Domain: web ingestion, SPA scraping, domain crawls, full-text search.

Target: 8 GB GPU running a 9B parameter model, no cloud, no API keys.
```

**Deployment scope:** local-first is the default — the pipeline above always runs
on local CPU with no cloud dependency. On top of that, this server can also run
in HTTP mode, self-hosted behind a reverse proxy, so it can be connected as a
remote endpoint by AI platforms and harnesses (Claude Desktop, claude.ai remote
MCP, other MCP clients) rather than only as a local stdio process. Remote mode is
opt-in, bearer-token authenticated (see §7), and still runs on infrastructure you
control. This is one of six sibling `MCP_*` repos brought to this same deployment
model.

## 2. Repository Structure

```
mcp_web_browser/
│
├── server.py                  ← MCP entrypoint. THIN. One-liner tools.
├── mcp.json                   ← self-updating launch entry
├── pyproject.toml
├── README.md
├── CLAUDE.md                  ← you are here
│
├── engine/                    ← pure Python. ZERO MCP imports.
│   ├── cli.py       optional standalone CLI (no MCP)
│   ├── core/        queue, router, scheduler, checkpoint, timer
│   ├── workers/     http, browser, crawl, search, fingerprint, tls
│   ├── resilience/  circuit_breaker, rate_limiter, retry
│   ├── db/          schema, indexer, query  (SQLite + FTS5, WAL mode)
│   ├── output/      stream (JSONL), export (CSV/JSON), display
│   ├── config/      defaults, domains
│   └── selectors/   per-domain CSS selectors (get(domain) → dict)
│
├── shared/                    ← cross-cutting helpers
│   ├── platform_utils.py    is_constrained_mode(), get_max_rows()
│   ├── path_safety.py       resolve_path()
│   └── version_control.py   snapshot() / atomic rename
│
└── tests/   unit/  smoke/  integration/  e2e/
```

## 3. Architecture Principles

### 3.1 Engine ↔ Server separation

```
engine/**           server.py
─────────           ─────────
no MCP imports      from mcp.server import Server
no stdout prints    @app.tool() one-liners only
sync core API       def browse_fetch(url): return engine.fetch_one(url)
unit-testable       no business logic here
without MCP
```

CI enforces this with:

```
grep -rE "^(from|import)\s+mcp" engine/ && exit 1
```

### 3.2 Just-in-time execution

Every MCP tool call is **one short bounded operation**. The model
orchestrates multi-step flows by calling small tools in sequence; the
engine never auto-loops behind the model's back, never spawns
background work that outlives the call (except `crawl_run`, which
checkpoints aggressively and is resumable).

### 3.3 Surgical reads

Tool responses are token-budgeted, not data-dumped:

- Read responses ≤ 500 tokens, write confirmations ≤ 150 tokens.
- Always include `truncated: bool` and `total: int` when the underlying
  result set is larger than the cap.
- Never return raw HTML, raw bytes, or full file contents — return
  identifiers and let `query_*` tools fetch slices on demand.

### 3.4 Snapshot before write

Every tool that mutates persistent state calls
`shared.version_control.snapshot()` first. Atomic rename pattern for
JSONL/checkpoint/router-cache files; SQLite uses WAL with
`busy_timeout=5000` and a final checkpoint at run end.

Coverage by module:

| Module                      | snapshot() call site          |
|-----------------------------|-------------------------------|
| `engine/__init__.py`        | before `sqlite3.connect()`    |
| `engine/__init__.py`        | before `query_export()` write |
| `engine/core/router.py`     | inside `_save_cache()`        |
| `engine/core/checkpoint.py` | inside `atomic_write_text()`  |
| `engine/output/export.py`   | before every file write       |
| `shared/version_control.py` | `atomic_write_bytes()` caller |

### 3.5 Chunked file writes (avoid single-shot timeouts)

Long single-shot file writes time out the API. Always build files in
parts:

1. `Write` the file with the title + first one or two sections only.
2. `Edit` (or append) one section at a time until the file is complete.
3. For remote pushes, upload an initial version via
   `create_or_update_file`, then issue follow-up commits that
   `create_or_update_file` again with the next chunk appended.

This applies to **every** large artefact in this repo: `PORT_PLAN.md`,
`README.md`, generated SQL schemas, long fixtures. Never paste a 300+
line file in one tool call.

### 3.6 Hardware-aware limits

Limits are **never hardcoded** in engine functions. Always go through
`shared.platform_utils`:

```python
from shared.platform_utils import get_max_rows, get_max_depth

def search(q: str, limit: int | None = None) -> list[dict]:
    cap = limit if limit is not None else get_max_rows()
    ...
```

`MCP_CONSTRAINED_MODE` is read **at call time**, not at import time, so
tests and CI can flip it without reloading modules.

## 4. Domain-Specific Tool Design Rules

### 4.1 LOCATE → INSPECT → PATCH → VERIFY

Every tier exposes the same four-role pattern:

| Role     | What it does                                | Mutates? |
|----------|---------------------------------------------|----------|
| LOCATE   | Probe / list / discover candidates           | no       |
| INSPECT  | Read a single item or a bounded slice         | no       |
| PATCH    | Execute one fetch / write / export            | yes      |
| VERIFY   | Read back the row / receipt of a prior PATCH  | no       |

If a proposed tool does not fit one of these roles, it does not belong
in the public surface. Compose two existing tools instead.

### 4.2 Tier discipline (≤ 8 tools per tier, ≤ 12 simultaneously loaded)

| Tier (env-toggled)              | Tools | Default |
|---------------------------------|-------|---------|
| `mcp_web_browser_basic`         | 8     | on      |
| `mcp_web_browser_query`         | 5     | on      |
| `mcp_web_browser_crawl`         | 6     | off     |

Basic + Query = 13 tools. On constrained hosts with a hard 12-tool
ceiling, disable Query (`MCP_TIER_QUERY=0`) to stay under the cap.
Never enable all three tiers at once.

`browse_research` lives in the crawl tier because it is a multi-step
deep-dive operation. It is the only tool in that tier that begins with
`browse_` instead of `crawl_`; this is intentional — it is a
cross-cutting research primitive, not a domain-crawler.

### 4.3 Tool schema discipline

- Tool docstrings ≤ **80 characters**. CI enforces:
  ```python
  assert all(len((t.__doc__ or "")) <= 80 for t in TOOLS)
  ```
- Tool parameters use Pydantic v2 models, not raw dict schemas.
- Default arguments resolved through `shared.platform_utils`, never
  baked into the schema.
- Error responses return `{ ok: False, error: "<slug>", hint: "..." }`
  — never raise across the MCP boundary.

### 4.4 Path & process safety

- Every user-supplied path: `shared.path_safety.resolve_path(p)`.
- Every subprocess call: `subprocess.run([...], shell=False)`.
- Playwright launches with sandbox **on** unless explicitly disabled
  by container detection.

## 5. What the AI Must NEVER Do

- Never `print()` to stdout anywhere a server import path can reach —
  it corrupts the MCP stdio framing. Use the engine's `display.py`
  helpers, which write to stderr.
- Never return a plain string from an MCP tool. Always a structured
  dict / Pydantic model.
- Never write to disk without calling `snapshot()` first.
- Never use `eval`, `exec`, `shell=True`, or string-formatted SQL.
- Never add a 9th tool to a tier or load all three tiers at once.
- Never import anything from `mcp.*` inside `engine/**` or `shared/**`.
- Never call a cloud API, require an API key, or add a network
  dependency that is not the target site itself.
- Never embed an LLM call inside the engine. The model is the caller,
  never the callee.
- Never hardcode row / depth / page caps. Go through
  `shared.platform_utils`.
- Never run a long-lived background task from inside a tool handler.
  `crawl_run` is the only exception, and it must checkpoint.
- Never write a long file in a single tool call — it will time out.
  Always use the chunked write protocol from §3.5 (Write a small
  header, then Edit/append one section at a time).
- Never rebase, force-push, or amend on shared branches.

## 6. Standard Workflow for a Change

1. Plan the change: which tier, which role (LOCATE/INSPECT/PATCH/VERIFY),
   which engine module.
2. Implement engine logic first under `engine/**`. Add unit tests that
   import nothing from `mcp.*`.
3. Wire the tool one-liner in `server.py`. Add a smoke test that
   round-trips a Pydantic-validated call.
4. Run in order — all must pass before committing:

   **Format:**
   ```
   uv run ruff format engine/ shared/ server.py tests/
   ```

   **Lint:**
   ```
   uv run ruff check engine/ shared/ server.py tests/
   ```

   **Type-check:**
   ```
   uv run pyright engine/ shared/ server.py
   ```

   **Test:**
   ```
   uv run pytest tests/ -q --tb=short
   ```

5. Commit with a message in the form `<area>: <imperative summary>`.
6. Update the progress tracker in §8 below.

## 7. Transport and Deployment (STANDARDS.md §30, §31)

Server supports `--transport stdio` (default, `server.py::main()`) and
`--transport http`, controlled by `WEB_HOST`/`WEB_PORT` env vars (read at
`FastMCP` construction time — not CLI flags; `--host`/`--port` do not exist).

For Docker/remote deployment — connecting this server to AI platforms and
harnesses as a hosted endpoint, not just a local LM Studio stdio process —
bearer auth (`deploy_auth.py`, `build_auth("WEB", host, port)`) gates the
whole server:

- `WEB_TOKENS_FILE` (named tokens, JSON `{name: token}`) — highest priority
- `WEB_TOKENS` (inline `"name:token,name2:token2"`)
- `WEB_API_KEY` (single shared token)
- unset = open mode (no auth) — localhost/private-network use only, never for
  a publicly reachable deployment

The production deployment runs `WEB_API_KEY` set from a local `.env` file
(gitignored, never committed) behind a reverse proxy; a request without a
valid `Authorization: Bearer <token>` header is rejected with `401` before it
reaches any tool.

`docker-compose.yml` runs one container (`mcp-web-browser`, `WEB_HOST=0.0.0.0`,
default port `8766`). `remote_launch.sh` is a Docker-free alternative (Colab /
any fresh Linux VM): runs `uv run python server.py --transport http` directly
plus a Cloudflare Quick Tunnel — NOT for production; unauthenticated at the
tunnel level unless `WEB_API_KEY` / `WEB_TOKENS*` is set first.

### Remote smoke tests (`remote_smoke_test.sh`)

Verifying a running HTTP endpoint (auth enforcement, real tool calls) is what
`remote_smoke_test.sh` covers, never storing the live API key in the repo.
`pytest` stays offline-only per STANDARDS.md.

It runs in **two** places: in CI via the `e2e` job, which starts the image with
`docker compose` and runs this script against `http://localhost:<port>` with a
throwaway key; and by hand against the deployment, which is still manual and
still never stores the live API key in the repo. CI must not be pointed at the
deployment -- CI runs on push and the redeploy happens after, so it would test
the old server against the new code. Assertions needing deployment-only
configuration (`MCP_FETCH_URLS`, a public base URL) skip in CI and run by hand.

Read values out of the envelope with `\\?"key\\?"[[:space:]]*:`. A tool's
document arrives as the JSON *string* `result.content[0].text`, so keys and
values are escaped (`\"result\": 93`). Patterns written for unescaped JSON
match nothing while every call still succeeds -- four of the six repos' scripts
had silently stopped asserting anything after the official-SDK migration
dropped `structuredContent`. Under `set -euo pipefail` an extractor matching
nothing also aborts the script before its own `|| fail` runs, so end one with
`|| true`.


`tests/test_smoke_test_covers_every_tool.py` keeps that script honest: it reads
every `@mcp.tool()` name out of the server modules' AST and fails if one never
appears in `remote_smoke_test.sh`. Offline, so it runs in CI. It exists because
a harness-driven sweep was once told to "list the tools then call each" for two
servers, listed some and called none -- 19 tools silently unexercised, with the
run still reporting a clean pass.

---

## 8. Progress Tracker

### 8.1 Milestones

- [x] **M1** — Repo scaffold (`pyproject.toml`, `uv.lock`, lint/pyright/pytest)
- [x] **M2** — `engine/db/` schema + indexer + query (SQLite + FTS5)
- [x] **M3** — `engine/workers/http_worker.py` + resilience layer
- [x] **M3b** — `engine/workers/search_worker.py` (SearXNG / DDG / Brave)
- [x] **M4** — `engine/workers/browser_worker.py` (Playwright stealth)
- [x] **M5** — `engine/workers/crawl_worker.py` + checkpoint resume
- [x] **M6** — `server.py` + Basic tier (6 tools incl. `browse_search`)
- [x] **M7** — Query tier + Crawl tier (10 more tools)
- [x] **M8** — `mcp.json` self-update flow + README + CI
- [x] **M9** — Gap-fill: core/ (queue, router, scheduler, timer), output/
               (stream, export, display), workers/tls, cli.py, selectors/,
               smoke/integration/e2e test suites

### 8.2 Tool surface

| Tier    | Tool             | Role     | Status |
|---------|------------------|----------|---------|
| basic   | `browse_search`  | LOCATE   | [x]    |
| basic   | `browse_locate`  | LOCATE   | [x]    |
| basic   | `browse_inspect` | INSPECT  | [x]    |
| basic   | `browse_fetch`   | PATCH    | [x]    |
| basic   | `browse_extract` | PATCH    | [x]    |
| basic   | `browse_verify`  | VERIFY   | [x]    |
| basic   | `browse_status`  | aux      | [x]    |
| basic   | `browse_datetime`| aux      | [x]    |
| query   | `query_locate`   | LOCATE   | [x]    |
| query   | `query_search`   | INSPECT  | [x]    |
| query   | `query_select`   | INSPECT  | [x]    |
| query   | `query_export`   | PATCH    | [x]    |
| query   | `query_stats`    | VERIFY   | [x]    |
| crawl   | `crawl_locate`   | LOCATE   | [x]    |
| crawl   | `crawl_plan`     | INSPECT  | [x]    |
| crawl   | `crawl_run`      | PATCH    | [x]    |
| crawl   | `crawl_resume`   | PATCH    | [x]    |
| crawl   | `crawl_verify`   | VERIFY   | [x]    |
| crawl   | `browse_research`| composite| [x]    |

### 8.3 Compliance gates (CI)

- [x] No `mcp` imports inside `engine/**` or `shared/**`
- [x] All tool docstrings ≤ 80 characters
- [x] Combined schema budget ≤ 700 tokens per tier
- [x] `MCP_CONSTRAINED_MODE` honoured at call time (test via monkeypatch)
- [x] `snapshot()` invoked before every persistent write (see §3.4 table)
- [x] No stdout writes from any module reachable by `server.py`

## 9. References

- Source engine: <https://github.com/azzindani/krawl>
- Krawl architecture: `docs/ARCHITECTURE.md` in the krawl repo
- Governing standard:
  <https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md>
