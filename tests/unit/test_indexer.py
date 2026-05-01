"""Verify Indexer routes results into correct tables atomically."""

from __future__ import annotations

import sqlite3

import pytest

from engine.db.indexer import Indexer, safe_float
from engine.db.schema import init_schema


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    return c


def test_safe_float_handles_suffixes() -> None:
    assert safe_float("1.5M") == 1_500_000
    assert safe_float("3,400") == 3400
    assert safe_float("12.5%") == 12.5
    assert safe_float(None) is None
    assert safe_float("") is None
    assert safe_float("not a number") is None


def test_index_page_writes_pages_and_fts(conn: sqlite3.Connection) -> None:
    idx = Indexer(conn)
    report = idx.index(
        {
            "url": "https://example.com/a",
            "title": "Example",
            "status": "ok",
            "mode": "http_curl",
            "elapsed_ms": 42,
            "extracted": {"text_preview": "hello world"},
        },
        run_id="run-1",
    )
    assert "page" in report.indexed
    page = conn.execute("SELECT * FROM pages WHERE url=?", ("https://example.com/a",)).fetchone()
    assert page is not None
    assert page["domain"] == "example.com"
    assert page["title"] == "Example"
    fts = conn.execute("SELECT * FROM fts_pages WHERE url=?", ("https://example.com/a",)).fetchone()
    assert fts is not None


def test_news_dedup_by_content_hash(conn: sqlite3.Connection) -> None:
    idx = Indexer(conn)
    payload = {
        "url": "https://news.example.com/feed",
        "group": "news",
        "extracted": {"headlines": ["Big story today", "Big story today"]},
    }
    idx.index(payload, "run-1")
    idx.index(payload, "run-1")  # second call must not duplicate
    n = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]
    assert n == 1


def test_stock_route_records_price(conn: sqlite3.Connection) -> None:
    idx = Indexer(conn)
    idx.index(
        {
            "url": "https://finance.yahoo.com/quote/BBCA.JK",
            "group": "Yahoo",
            "ticker": "BBCA.JK",
            "extracted": {"price": "9,500", "volume": "1.2M"},
        },
        "run-1",
    )
    row = conn.execute("SELECT * FROM stocks WHERE ticker=?", ("BBCA.JK",)).fetchone()
    assert row is not None
    assert row["price"] == 9500.0
    assert row["volume"] == 1_200_000.0


def test_links_dedup_pair(conn: sqlite3.Connection) -> None:
    idx = Indexer(conn)
    idx.index(
        {
            "url": "https://a.example.com/",
            "links": ["https://b.example.com/x", "https://b.example.com/x"],
        },
        "run-1",
    )
    n = conn.execute("SELECT COUNT(*) AS n FROM links").fetchone()["n"]
    assert n == 1


def test_domain_stats_increment(conn: sqlite3.Connection) -> None:
    idx = Indexer(conn)
    for i in range(3):
        idx.index({"url": f"https://x.example.com/{i}", "elapsed_ms": 100}, "r")
    row = conn.execute("SELECT * FROM domains WHERE domain=?", ("x.example.com",)).fetchone()
    assert row["total_pages"] == 3
