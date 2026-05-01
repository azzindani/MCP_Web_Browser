"""Optional standalone CLI. No MCP dependencies.

Usage (from repo root)::

    uv run python -m engine.cli --url https://example.com
    uv run python -m engine.cli --tasks tasks/example.json
    uv run python -m engine.cli --domain example.com --depth 2
    uv run python -m engine.cli --search "suku bunga" --db krawl.db
    uv run python -m engine.cli --stats --db krawl.db
    uv run python -m engine.cli --export pages --fmt csv --db krawl.db

This file is intentionally thin — all logic lives in the Scheduler and
engine entry-points so MCP tools and CLI share the same code paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from engine.core.scheduler import Scheduler, SchedulerConfig
from engine.db.query import QueryEngine
from engine.db.schema import init_schema
from shared.path_safety import resolve_path


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── argument parser ───────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="krawl",
        description="mcp_web_browser engine — standalone CLI",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--url", metavar="URL", help="Fetch a single URL")
    g.add_argument("--tasks", metavar="FILE", help="JSON task list file")
    g.add_argument("--domain", metavar="DOMAIN", help="Domain sweep (crawl mode)")
    g.add_argument("--search", metavar="QUERY", help="FTS query against local DB")
    g.add_argument("--export", metavar="TABLE", help="Export table to file")
    g.add_argument("--stats", action="store_true", help="Print DB stats and exit")

    p.add_argument("--db", default="krawl.db", help="SQLite path")
    p.add_argument("--out", default="krawl_output.jsonl", help="JSONL output path")
    p.add_argument("--depth", type=int, default=2, help="Crawl depth")
    p.add_argument("--limit", type=int, default=50, help="Max rows / pages")
    p.add_argument("--fmt", default="csv", help="Export format: csv|json")
    p.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    p.add_argument("--instance", default="default", help="Instance ID")
    return p


# ── sub-commands ────────────────────────────────────────────────


def _open_db(db_path: str) -> tuple[sqlite3.Connection, QueryEngine]:
    path = resolve_path(db_path)
    if not path.exists():
        _stderr(f"database not found: {path}")
        sys.exit(1)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn, QueryEngine(conn)


def _cmd_stats(db_path: str) -> None:
    conn, qe = _open_db(db_path)
    for k, v in qe.stats().items():
        print(f"{k:<25}: {v}")
    conn.close()


def _cmd_search(db_path: str, query: str, limit: int) -> None:
    conn, qe = _open_db(db_path)
    rows = qe.search(query, limit=limit)
    print(json.dumps(rows, indent=2, default=str))
    conn.close()


def _cmd_export(db_path: str, table: str, fmt: str, out_dir: str = ".") -> None:
    conn, qe = _open_db(db_path)
    from engine.output.export import Exporter

    exp = Exporter(qe)
    if fmt == "json":
        path = exp.export_json(table, out_dir)
    else:
        path = exp.export_csv(table, out_dir)
    if path:
        print(path)
    else:
        _stderr(f"no rows in table '{table}'")
    conn.close()


async def _run_tasks(args: argparse.Namespace, inputs: list[dict]) -> None:  # type: ignore[type-arg]
    cfg = SchedulerConfig(
        db_path=args.db,
        jsonl_path=args.out,
        instance_id=args.instance,
        resume=args.resume,
    )
    sched = Scheduler(cfg)
    sched.add_tasks(inputs)
    await sched.run()


# ── entry point ──────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.stats:
        _cmd_stats(args.db)
        return

    if args.search:
        _cmd_search(args.db, args.search, args.limit)
        return

    if args.export:
        _cmd_export(args.db, args.export, args.fmt)
        return

    inputs: list[dict] = []  # type: ignore[type-arg]
    if args.url:
        inputs = [{"url": args.url, "mode": "auto"}]
    elif args.tasks:
        p = Path(args.tasks)
        if not p.exists():
            _stderr(f"task file not found: {p}")
            sys.exit(1)
        data = json.loads(p.read_text(encoding="utf-8"))
        inputs = data if isinstance(data, list) else [data]
    elif args.domain:
        url = args.domain if args.domain.startswith("http") else f"https://{args.domain}"
        inputs = [{"url": url, "mode": "crawl", "crawl_depth": args.depth, "name": args.domain}]
    else:
        parser.print_help()
        return

    asyncio.run(_run_tasks(args, inputs))


if __name__ == "__main__":
    main()
