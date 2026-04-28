"""Integration tests. Real SQLite in-memory, no network, no Playwright.

Verifies that indexer → query → export round-trips work end-to-end.
Marker: integration (runs in CI without external services).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine.db.indexer import Indexer
from engine.db.query import QueryEngine
from engine.db.schema import init_schema
from engine.output.export import Exporter


# ── fixtures ───────────────────────────────────────────────────

@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


@pytest.fixture()
def indexer(db: sqlite3.Connection) -> Indexer:
    return Indexer(db)


@pytest.fixture()
def query(db: sqlite3.Connection) -> QueryEngine:
    return QueryEngine(db)


# ── page indexing ─────────────────────────────────────────────

def test_index_page_and_fts_search(
    indexer: Indexer, query: QueryEngine
) -> None:
    result = {
        "url": "https://example.com/article",
        "title": "Test Article",
        "status": "ok",
        "mode": "http_curl",
        "elapsed_ms": 250,
        "extracted": {"text_preview": "Hello world from the test article body"},
        "group": "test",
    }
    report = indexer.index(result, "run_test")
    assert "page" in report.indexed

    rows = query.search("hello world")
    assert any(r["url"] == "https://example.com/article" for r in rows)


def test_upsert_updates_last_seen(
    indexer: Indexer, db: sqlite3.Connection
) -> None:
    base = {
        "url": "https://upsert.com/",
        "title": "First visit",
        "status": "ok",
        "mode": "http_curl",
        "elapsed_ms": 100,
        "extracted": {},
    }
    indexer.index(base, "run_1")
    base["title"] = "Second visit"
    indexer.index(base, "run_2")

    rows = list(db.execute("SELECT COUNT(*) AS n FROM pages WHERE url=?", (base["url"],)))
    assert rows[0]["n"] == 1  # upsert — still one row


# ── stock indexing ─────────────────────────────────────────────

def test_index_stock_row(
    indexer: Indexer, db: sqlite3.Connection
) -> None:
    result = {
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/BBCA.JK",
        "title": "Bank BCA",
        "status": "ok",
        "mode": "http_json",
        "elapsed_ms": 180,
        "group": "Yahoo",
        "ticker": "BBCA",
        "extracted": {"price": 9450.0, "change": 50.0, "changePct": 0.53},
    }
    indexer.index(result, "run_test")
    rows = list(db.execute("SELECT * FROM stocks WHERE ticker='BBCA'"))
    assert len(rows) >= 1
    assert float(rows[0]["price"]) == pytest.approx(9450.0)


# ── news indexing ─────────────────────────────────────────────

def test_index_news_headlines(
    indexer: Indexer, query: QueryEngine
) -> None:
    result = {
        "url": "https://news.com/",
        "title": "News Portal",
        "status": "ok",
        "mode": "browser",
        "elapsed_ms": 800,
        "extracted": {"headlines": ["Economy grows strongly in Q2", "Rate hike expected soon"]},
        "group": "bisnis",
    }
    report = indexer.index(result, "run_test")
    assert any("news" in r for r in report.indexed)

    rows = query.search("economy grows", table="fts_news")
    assert len(rows) >= 1


def test_news_dedup_by_content_hash(
    indexer: Indexer, db: sqlite3.Connection
) -> None:
    result = {
        "url": "https://news.com/",
        "title": "Portal",
        "status": "ok",
        "mode": "browser",
        "elapsed_ms": 100,
        "extracted": {"headlines": ["Duplicate headline test"]},
        "group": "test",
    }
    indexer.index(result, "r1")
    indexer.index(result, "r2")  # same headline — should not duplicate
    rows = list(db.execute("SELECT COUNT(*) AS n FROM news WHERE headline=?", ("Duplicate headline test",)))
    assert rows[0]["n"] == 1


# ── links ─────────────────────────────────────────────────────

def test_index_links(indexer: Indexer, db: sqlite3.Connection) -> None:
    result = {
        "url": "https://parent.com/",
        "status": "ok",
        "mode": "crawl",
        "elapsed_ms": 200,
        "links": ["https://child.com/a", "https://child.com/b"],
        "extracted": {},
    }
    indexer.index(result, "run_test")
    rows = list(db.execute("SELECT * FROM links WHERE from_url=?", ("https://parent.com/",)))
    assert len(rows) == 2


# ── query helpers ─────────────────────────────────────────────

def test_query_stats_reflects_indexed(
    indexer: Indexer, query: QueryEngine
) -> None:
    for i in range(3):
        indexer.index(
            {"url": f"https://stats.com/{i}", "status": "ok",
             "mode": "http_curl", "elapsed_ms": 50, "extracted": {}},
            "run_stats",
        )
    stats = query.stats()
    assert int(stats.get("pages", 0)) >= 3


def test_query_select_whitelist(query: QueryEngine) -> None:
    rows = query.select("SELECT COUNT(*) AS n FROM pages", limit=1)
    assert isinstance(rows, list)
    assert "n" in rows[0]


def test_query_select_blocks_ddl(query: QueryEngine) -> None:
    with pytest.raises(ValueError, match="SELECT"):
        query.select("DROP TABLE pages")


def test_query_select_blocks_dml(query: QueryEngine) -> None:
    with pytest.raises(ValueError, match="SELECT"):
        query.select("DELETE FROM pages")


def test_query_search_bad_table(query: QueryEngine) -> None:
    with pytest.raises(ValueError):
        query.search("test", table="nonexistent_table")


# ── export ───────────────────────────────────────────────────

def test_export_csv(
    indexer: Indexer, query: QueryEngine, tmp_path: Path
) -> None:
    indexer.index(
        {"url": "https://export-test.com/", "title": "Export Page",
         "status": "ok", "mode": "http_curl", "elapsed_ms": 100, "extracted": {}},
        "run_export",
    )
    exp = Exporter(query)
    out = exp.export_csv("pages", tmp_path)
    assert out != ""
    content = (tmp_path / "pages.csv").read_text()
    assert "export-test.com" in content


def test_export_json(
    indexer: Indexer, query: QueryEngine, tmp_path: Path
) -> None:
    indexer.index(
        {"url": "https://json-export.com/", "title": "JSON Export",
         "status": "ok", "mode": "http_curl", "elapsed_ms": 90, "extracted": {}},
        "run_json_export",
    )
    exp = Exporter(query)
    out = exp.export_json("pages", tmp_path)
    import json
    rows = json.loads((tmp_path / "pages.json").read_text())
    assert any(r["url"] == "https://json-export.com/" for r in rows)


# ── domain stats ─────────────────────────────────────────────

def test_domain_table_populated(
    indexer: Indexer, db: sqlite3.Connection
) -> None:
    indexer.index(
        {"url": "https://domain-track.com/page", "status": "ok",
         "mode": "http_curl", "elapsed_ms": 150, "extracted": {}},
        "run_domain",
    )
    rows = list(db.execute(
        "SELECT * FROM domains WHERE domain=?", ("domain-track.com",)
    ))
    assert len(rows) == 1
    assert int(rows[0]["total_pages"]) >= 1
