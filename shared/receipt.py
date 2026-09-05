"""Append-only JSON receipt log for web engine write operations.

Stored at <db_path>.mcp_receipt.json — one entry per write op.
Read-only ops (search, inspect, probe, verify, query) are NOT logged.
Write ops: browse_fetch, crawl_run, query_export.

**This file had no reader.**

Four servers in this fleet write `{file}.mcp_receipt.json`; this one wrote it
and offered nothing to read it back. A log with no supported way in is a log
whose format is whatever the next person guesses, and the guessing had already
started: MCP_Data_Analyst, MCP_Machine_Learning and MCP_File_System write a
JSON array, MCP_Microsoft_Office wrote a JSON object, and no two of them could
read each other's file for the same document. `read_receipt` here is the same
function the siblings expose, reading the same shapes.

**What the log records, and why the file says so.**

A user review drove roughly twenty calls at one file, read the receipt, found
two entries, and concluded that eighteen operations had never run. The log was
correct -- it holds writes, and reads change nothing -- but nothing in it said
so, and a file called `.mcp_receipt.json` invites exactly one reading.
`RECEIPT_SCOPE` is that sentence, stored in the file and returned beside the
entries, so a short log can be understood instead of distrusted.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from shared.version_control import atomic_write_text

# Identical wording to the siblings: they write the same filename convention,
# and a caller reading the scope should not be able to tell which server wrote
# it. The ops named here are this server's, so the sentence stays general.
RECEIPT_SCOPE = (
    "mutations only: operations that wrote to this file. Reads, inspections, "
    "correlations and chart generation are not recorded here."
)

# Above this, a content hash costs more than the operation it describes. A
# crawl database is routinely larger than this, which is the case the cap is
# for -- a fingerprint that costs more than the crawl is not a fingerprint
# anyone will keep.
_MAX_HASH_BYTES = 64 * 1024 * 1024


def _receipt_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".mcp_receipt.json")


def _hash_args(args: dict[str, Any]) -> str:
    """Stable hash of the arguments, so two calls can be told apart."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(sorted(args.items()))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def fingerprint(file_path: str | Path) -> str:
    """Identify a file's contents, or say honestly that this is not a hash.

    Returns `sha256:<16 hex>` for a file small enough to read, and
    `size-mtime:<...>` for one that is not. The prefix is the point: a caller
    comparing two fingerprints must be able to tell a content hash from a
    cheaper stand-in, because only one of them proves the bytes are the same.
    """
    p = Path(file_path)
    try:
        stat = p.stat()
    except OSError:
        return ""
    if stat.st_size > _MAX_HASH_BYTES:
        return f"size-mtime:{stat.st_size}-{int(stat.st_mtime)}"
    try:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return f"size-mtime:{stat.st_size}-{int(stat.st_mtime)}"
    return f"sha256:{digest}"


def _split_header(loaded: Any) -> tuple[list[dict], dict | None]:
    """Entries and scope header, from every shape the fleet has written.

    * headed array  -- `[{"_scope": ...}, entry, ...]`, what this writes now
    * bare array    -- what this and the siblings wrote before the header
    * legacy object -- `{"file": ..., "entries": [...]}`, Office's old form
    """
    if isinstance(loaded, dict):
        entries = loaded.get("entries", [])
        return [e for e in entries if isinstance(e, dict)], None
    if not isinstance(loaded, list) or not loaded:
        return [], None
    first = loaded[0]
    if isinstance(first, dict) and "_scope" in first:
        return [e for e in loaded[1:] if isinstance(e, dict)], first
    return [e for e in loaded if isinstance(e, dict)], None


def append_receipt(
    db_path: str | Path,
    op: str,
    args: dict[str, Any],
    result: str,
    backup: str | None = None,
    input_fingerprint: str = "",
    duration_ms: float | None = None,
) -> None:
    """Append one entry to <db_path>.mcp_receipt.json.

    `input_fingerprint` is what `fingerprint()` returned BEFORE the write; the
    output side is measured here, after it. Omit it and the entry is still
    valid -- one side of a lineage is better than none, and no call site is
    obliged to change.
    """
    receipt_path = _receipt_path(db_path)
    entries: list[dict[str, Any]] = []
    header: dict[str, Any] | None = None
    if receipt_path.exists():
        try:
            entries, header = _split_header(json.loads(receipt_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError, OSError:
            entries, header = [], None
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "op": op,
        "args": args,
        "args_hash": _hash_args(args),
        "result": result,
    }
    if backup:
        entry["backup"] = backup
    if input_fingerprint:
        entry["input"] = input_fingerprint
    after = fingerprint(db_path)
    if after:
        entry["output"] = after
    if duration_ms is not None:
        entry["duration_ms"] = round(float(duration_ms), 1)
    entries.append(entry)
    head = header or {"_scope": RECEIPT_SCOPE, "_format": 2}
    atomic_write_text(receipt_path, json.dumps([head, *entries], indent=2, default=str))


def read_receipt_log(db_path: str | Path, last_n: int = 10) -> list[dict[str, Any]]:
    """Return receipt entries, oldest first. Empty list if no receipt exists."""
    entries, _ = read_receipt(db_path, last_n)
    return entries


def read_receipt(db_path: str | Path, last_n: int = 10) -> tuple[list[dict[str, Any]], str]:
    """The last N entries and the scope sentence that belongs beside them.

    Two return values rather than one because the count alone is what misled a
    caller: twenty operations, two entries, and no way to learn from the log
    that eighteen of them were never eligible for it.
    """
    receipt_path = _receipt_path(db_path)
    if not receipt_path.exists():
        return [], RECEIPT_SCOPE
    try:
        loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return [], RECEIPT_SCOPE
    entries, header = _split_header(loaded)
    scope = str(header.get("_scope")) if header else RECEIPT_SCOPE
    return (entries[-last_n:] if last_n > 0 else entries), scope
