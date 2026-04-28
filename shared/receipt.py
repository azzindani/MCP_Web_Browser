"""Append-only JSON receipt log for web engine write operations.

Stored at <db_path>.mcp_receipt.json — one entry per write op.
Read-only ops (search, inspect, probe, verify, query) are NOT logged.
Write ops: browse_fetch, crawl_run, query_export.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from shared.version_control import atomic_write_text


def append_receipt(
    db_path: str | Path,
    op: str,
    args: dict[str, Any],
    result: str,
    backup: str | None = None,
) -> None:
    """Append one entry to <db_path>.mcp_receipt.json."""
    receipt_path = Path(str(db_path) + ".mcp_receipt.json")
    entries: list[dict[str, Any]] = []
    if receipt_path.exists():
        try:
            entries = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "op": op,
        "args": args,
        "result": result,
    }
    if backup:
        entry["backup"] = backup
    entries.append(entry)
    atomic_write_text(receipt_path, json.dumps(entries, indent=2, default=str))
