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
