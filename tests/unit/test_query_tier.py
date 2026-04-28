"""Verify query-tier engine entry points: locate, search, select, export, stats."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import engine
from engine.db.indexer import Indexer
from engine.db.schema import init_schema


@pytest.fixture()
def isolated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    engine.reset_runtime()
    rt = engine.runtime()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    rt._db = conn

    # Seed pages directly via the indexer so FTS content is populated.
    idx = Indexer(conn)
    idx.index(
        {
            "url": "https://example.com/banking",
            "title": "Banking news",
            "extracted": {"text_preview": "interest rates rose this week"},
        },
        run_id="seed",
    )
    idx.index(
        {
            "url": "https://example.com/sports",
            "title": "Football",
            "extracted": {"text_preview": "team won the cup"},
        },
        run_id="seed",
    )

    yield None
    if rt._db is not None:
        rt._db.close()
        rt._db = None
    engine.reset_runtime()


def test_query_locate_lists_tables(isolated_engine: None) -> None:
    out = engine.query_locate()
    assert out["ok"] is True
    assert out["tables"]["pages"] >= 1
    assert "stocks" in out["tables"]


def test_query_search_finds_indexed_page(isolated_engine: None) -> None:
    out = engine.query_search("interest", table="fts_pages")
    assert out["ok"] is True
    assert out["table"] == "fts_pages"
    assert any("banking" in r["url"] for r in out["rows"])


def test_query_search_rejects_unknown_table(isolated_engine: None) -> None:
    out = engine.query_search("anything", table="bogus")
    assert out["ok"] is False
    assert "fts_" in out["hint"]


def test_query_select_rejects_non_select(isolated_engine: None) -> None:
    out = engine.query_select("DROP TABLE pages")
    assert out["ok"] is False
    assert "SELECT" in out["hint"] or "WITH" in out["hint"]


def test_query_select_returns_rows(isolated_engine: None) -> None:
    out = engine.query_select("SELECT url FROM pages")
    assert out["ok"] is True
    assert out["total"] >= 1


def test_query_export_writes_csv(isolated_engine: None, tmp_path: Path) -> None:
    out_path = "out.csv"
    out = engine.query_export("pages", out_path, fmt="csv")
    assert out["ok"] is True
    text = (tmp_path / "out.csv").read_text(encoding="utf-8")
    assert text.startswith("id,url,domain")


def test_query_export_rejects_unknown_table(
    isolated_engine: None,
) -> None:
    out = engine.query_export("nope", "x.csv")
    assert out["ok"] is False
    assert out["error"] == "unknown_table"


def test_query_stats_includes_db_bytes(isolated_engine: None) -> None:
    s = engine.query_stats()
    assert s["ok"] is True
    assert "db_bytes" in s
