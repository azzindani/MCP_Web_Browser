"""Verify init_schema applies pragmas and creates every expected table."""

from __future__ import annotations

import sqlite3

import pytest

from engine.db.schema import init_schema

EXPECTED_TABLES = {
    "runs", "task_log", "pages", "stocks", "news", "market_indices",
    "files", "links", "domains", "endpoints", "selector_store",
}
EXPECTED_FTS = {"fts_pages", "fts_news", "fts_files"}


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    init_schema(c)
    return c


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert EXPECTED_TABLES.issubset(names)
    assert EXPECTED_FTS.issubset(names)


def test_pragmas_applied(conn: sqlite3.Connection) -> None:
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    # In-memory dbs report 'memory' for journal_mode regardless; the call
    # itself succeeding is what matters here. Validate cache_size landed.
    cs = conn.execute("PRAGMA cache_size").fetchone()[0]
    assert cs == -64000


def test_init_is_idempotent() -> None:
    c = sqlite3.connect(":memory:")
    init_schema(c)
    init_schema(c)  # must not raise
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert {r[0] for r in rows} >= EXPECTED_TABLES
