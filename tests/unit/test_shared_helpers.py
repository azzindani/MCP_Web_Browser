"""Unit tests for shared/progress.py, shared/receipt.py, shared/handover.py.

All pure-logic tests — no network, no MCP, no I/O except tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.handover import make_handover, next_step
from shared.progress import fail, info, ok, undo, warn
from shared.receipt import append_receipt

# ── progress helpers ───────────────────────────────────────────────────────────


def test_ok_returns_correct_status() -> None:
    r = ok("Loaded", "5 rows")
    assert r == {"status": "ok", "label": "Loaded", "detail": "5 rows"}


def test_fail_returns_correct_status() -> None:
    r = fail("Not found", "sales.csv")
    assert r["status"] == "fail"
    assert r["label"] == "Not found"


def test_info_returns_correct_status() -> None:
    assert info("Snapshot")["status"] == "info"


def test_warn_returns_correct_status() -> None:
    assert warn("Large dataset")["status"] == "warn"


def test_undo_returns_correct_status() -> None:
    assert undo("Restored backup")["status"] == "undo"


def test_detail_defaults_to_empty_string() -> None:
    r = ok("label only")
    assert r["detail"] == ""


def test_all_helpers_return_dicts() -> None:
    for fn in (ok, fail, info, warn, undo):
        result = fn("label", "detail")
        assert isinstance(result, dict)
        assert set(result.keys()) == {"status", "label", "detail"}


# ── receipt ──────────────────────────────────────────────────────────────────


def test_receipt_creates_file(tmp_path: Path) -> None:
    db = tmp_path / "krawl.db"
    append_receipt(db, op="browse_fetch", args={"url": "https://x.com"}, result="ok")
    receipt = Path(str(db) + ".mcp_receipt.json")
    assert receipt.exists()


def test_receipt_appends_entries(tmp_path: Path) -> None:
    db = tmp_path / "krawl.db"
    append_receipt(db, op="browse_fetch", args={"url": "https://a.com"}, result="ok")
    append_receipt(db, op="crawl_run", args={"url": "https://b.com"}, result="5 pages")
    entries = json.loads(Path(str(db) + ".mcp_receipt.json").read_text())
    assert len(entries) == 2
    assert entries[0]["op"] == "browse_fetch"
    assert entries[1]["op"] == "crawl_run"


def test_receipt_entry_fields(tmp_path: Path) -> None:
    db = tmp_path / "krawl.db"
    append_receipt(
        db,
        op="query_export",
        args={"table": "pages", "fmt": "csv"},
        result="1000 bytes",
        backup="/tmp/backup.bak",
    )
    entries = json.loads(Path(str(db) + ".mcp_receipt.json").read_text())
    e = entries[0]
    assert "ts" in e
    assert e["backup"] == "/tmp/backup.bak"
    assert e["args"]["table"] == "pages"


def test_receipt_no_backup_field_when_omitted(tmp_path: Path) -> None:
    db = tmp_path / "krawl.db"
    append_receipt(db, op="browse_fetch", args={}, result="ok")
    entries = json.loads(Path(str(db) + ".mcp_receipt.json").read_text())
    assert "backup" not in entries[0]


def test_receipt_survives_corrupt_json(tmp_path: Path) -> None:
    db = tmp_path / "krawl.db"
    receipt = Path(str(db) + ".mcp_receipt.json")
    receipt.write_text("INVALID JSON")  # corrupt the file
    # should not raise; starts fresh
    append_receipt(db, op="browse_fetch", args={}, result="ok")
    entries = json.loads(receipt.read_text())
    assert len(entries) == 1


# ── handover ─────────────────────────────────────────────────────────────────


def test_next_step_returns_dict() -> None:
    s = next_step("browse_fetch", "fetch the URL")
    assert s == {"tool": "browse_fetch", "reason": "fetch the URL"}


def test_make_handover_empty() -> None:
    h = make_handover()
    assert h == {}


def test_make_handover_with_suggested_next() -> None:
    h = make_handover(
        suggested_next=[next_step("browse_fetch", "index it")],
    )
    assert "suggested_next" in h
    assert h["suggested_next"][0]["tool"] == "browse_fetch"


def test_make_handover_with_carry_forward() -> None:
    h = make_handover(carry_forward={"url": "https://x.com"})
    assert h["carry_forward"]["url"] == "https://x.com"
    assert "suggested_next" not in h


def test_make_handover_full() -> None:
    h = make_handover(
        suggested_next=[next_step("query_search", "search it")],
        carry_forward={"run_id": "abc"},
    )
    assert len(h["suggested_next"]) == 1
    assert h["carry_forward"]["run_id"] == "abc"
