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
the ability to fetch web pages, run a stealth browser, crawl domains,
and query the resulting SQLite knowledge base.

```
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
│   ├── workers/     http, browser, crawl, fingerprint, tls
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
