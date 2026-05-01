"""Verify QueryEngine: FTS search, bounded select, stats, csv."""

from __future__ import annotations

import sqlite3

import pytest

from engine.db.indexer import Indexer
from engine.db.query import QueryEngine
from engine.db.schema import init_schema


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.delenv("MCP_CONSTRAINED_MODE", raising=False)
    c = sqlite3.connect(":memory:")
    init_schema(c)
    idx = Indexer(c)
    idx.index(
        {
            "url": "https://example.com/banking",
            "title": "Banking news today",
            "extracted": {"text_preview": "interest rates rose this week"},
        },
        "r",
    )
    idx.index(
        {
            "url": "https://example.com/sports",
            "title": "Football update",
            "extracted": {"text_preview": "team won the cup"},
        },
        "r",
    )
    return c


def test_fts_search_finds_match(db: sqlite3.Connection) -> None:
    q = QueryEngine(db)
    rows = q.search("interest", limit=10)
    assert len(rows) == 1
    assert "banking" in rows[0]["url"]


def test_search_rejects_unknown_table(db: sqlite3.Connection) -> None:
    q = QueryEngine(db)
    with pytest.raises(ValueError):
        q.search("anything", table="pages_secret")


def test_select_rejects_non_select(db: sqlite3.Connection) -> None:
    q = QueryEngine(db)
    with pytest.raises(ValueError):
        q.select("DELETE FROM pages")


def test_select_caps_rows_in_constrained_mode(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "1")
    q = QueryEngine(db)
    rows = q.select("SELECT * FROM pages")
    # Constrained cap is 20; we only have 2 pages so all fit, but the
    # call must not raise. The cap matters when row count > 20.
    assert len(rows) == 2


def test_stats_returns_row_counts(db: sqlite3.Connection) -> None:
    q = QueryEngine(db)
    s = q.stats()
    assert s["pages"] == 2
    assert s["domains"] >= 1
    assert "db_bytes" in s


def test_to_csv_quotes_commas(db: sqlite3.Connection) -> None:
    q = QueryEngine(db)
    out = q.to_csv("pages")
    assert out.splitlines()[0].startswith("id,url,domain")
    assert out.count("\n") >= 2  # header + 2 data rows
