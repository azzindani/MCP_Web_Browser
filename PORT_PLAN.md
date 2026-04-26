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
One engine. Any URL. Any scale. Zero LLM in the engine. Zero cloud.
The model decides what to fetch and when. The MCP server fetches it
synchronously, returns a surgical confirmation, and persists the
result to a local SQLite file the model can query later.
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
