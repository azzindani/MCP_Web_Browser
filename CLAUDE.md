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

The full migration plan is in `PORT_PLAN.md`. Read it before designing
new modules.

## 2. Repository Structure

```
mcp_web_browser/
│
├── server.py                  ← MCP entrypoint. THIN. One-liner tools.
├── mcp.json                   ← self-updating launch entry
├── pyproject.toml
├── README.md
├── CLAUDE.md                  ← you are here
├── PORT_PLAN.md
│
├── engine/                    ← pure Python. ZERO MCP imports.
│   ├── core/        queue, router, scheduler, checkpoint, timer
│   ├── workers/     http, browser, crawl, search, fingerprint, tls
│   ├── resilience/  circuit_breaker, rate_limiter, retry
│   ├── db/          schema, indexer, query  (SQLite + FTS5, WAL mode)
│   ├── output/      stream (JSONL), export (CSV/JSON), display
│   ├── config/      defaults, domains
│   └── selectors/   per-domain extraction selectors
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
| `mcp_web_browser_basic`         | 6     | on      |
| `mcp_web_browser_query`         | 5     | on      |
| `mcp_web_browser_crawl`         | 5     | off     |

Never enable all three tiers at once on a constrained machine — that
breaks the 12-tool ceiling once the host (LM Studio etc.) adds its own
built-ins.

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
- Never push to `main`. Development happens on
  `claude/port-krawl-mcp-fY8xW`.

## 6. Standard Workflow for a Change

1. Read `PORT_PLAN.md` § matching the milestone you are working on.
2. Plan the change: which tier, which role (LOCATE/INSPECT/PATCH/VERIFY),
   which engine module.
3. Implement engine logic first under `engine/**`. Add unit tests that
   import nothing from `mcp.*`.
4. Wire the tool one-liner in `server.py`. Add a smoke test that
   round-trips a Pydantic-validated call.
5. Run:
   ```
   uv run ruff check .
   uv run mypy engine shared server.py
   uv run pytest -q
   ```
6. Commit on `claude/port-krawl-mcp-fY8xW` with a message in the form
   `<area>: <imperative summary>`.
7. Update the progress tracker in §7 below.

## 7. Progress Tracker

### 7.1 Milestones

- [ ] **M1** — Repo scaffold (`pyproject.toml`, `uv.lock`, lint/mypy/pytest)
- [ ] **M2** — `engine/db/` schema + indexer + query (SQLite + FTS5)
- [ ] **M3** — `engine/workers/http_worker.py` + resilience layer
- [ ] **M3b** — `engine/workers/search_worker.py` (SearXNG / DDG / Brave)
- [ ] **M4** — `engine/workers/browser_worker.py` (Playwright stealth)
- [ ] **M5** — `engine/workers/crawl_worker.py` + checkpoint resume
- [ ] **M6** — `server.py` + Basic tier (6 tools incl. `browse_search`)
- [ ] **M7** — Query tier + Crawl tier (10 more tools)
- [ ] **M8** — `mcp.json` self-update flow + README + CI

### 7.2 Tool surface

| Tier    | Tool             | Role     | Status |
|---------|------------------|----------|--------|
| basic   | `browse_search`  | LOCATE   | [ ]    |
| basic   | `browse_locate`  | LOCATE   | [ ]    |
| basic   | `browse_inspect` | INSPECT  | [ ]    |
| basic   | `browse_fetch`   | PATCH    | [ ]    |
| basic   | `browse_verify`  | VERIFY   | [ ]    |
| basic   | `browse_status`  | aux      | [ ]    |
| query   | `query_locate`   | LOCATE   | [ ]    |
| query   | `query_search`   | INSPECT  | [ ]    |
| query   | `query_select`   | INSPECT  | [ ]    |
| query   | `query_export`   | PATCH    | [ ]    |
| query   | `query_stats`    | VERIFY   | [ ]    |
| crawl   | `crawl_locate`   | LOCATE   | [ ]    |
| crawl   | `crawl_plan`     | INSPECT  | [ ]    |
| crawl   | `crawl_run`      | PATCH    | [ ]    |
| crawl   | `crawl_resume`   | PATCH    | [ ]    |
| crawl   | `crawl_verify`   | VERIFY   | [ ]    |

### 7.3 Compliance gates (CI)

- [ ] No `mcp` imports inside `engine/**` or `shared/**`
- [ ] All tool docstrings ≤ 80 characters
- [ ] Combined schema budget ≤ 700 tokens per tier
- [ ] `MCP_CONSTRAINED_MODE` honoured at call time (test via monkeypatch)
- [ ] `snapshot()` invoked before every persistent write (audit script)
- [ ] No stdout writes from any module reachable by `server.py`

## 8. References

- Migration plan: `PORT_PLAN.md`
- Source engine: <https://github.com/azzindani/krawl>
- Krawl architecture: `docs/ARCHITECTURE.md` in the krawl repo
- Governing standard:
  <https://github.com/azzindani/Standards/blob/main/local_mcp/STANDARDS.md>
