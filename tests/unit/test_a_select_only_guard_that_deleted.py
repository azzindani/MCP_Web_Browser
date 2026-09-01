"""query_select said SELECT-only and would DELETE every row.

The guard was a test on the first word:

    stripped.startswith("select") or stripped.startswith("with")

SQLite puts a `WITH` clause in front of DELETE, INSERT and UPDATE as ordinary
documented syntax, so

    WITH x AS (SELECT 1) DELETE FROM pages

begins with `with`, is a *single* statement -- so sqlite3's one-statement rule
does not catch it either -- and empties the table. `ok: true`, progress
"SQL executed". Confirmed against the deployment with a `WHERE 1=0` appended so
the proof could not destroy the live crawl index.

Nothing downstream stopped it: the query tier runs on the same read-write
connection the indexer writes through.

The guarantee is now `set_authorizer`, which SQLite consults while compiling and
hands the parsed action, not a prefix.
"""

from __future__ import annotations

import sqlite3

import pytest

from engine.db.indexer import Indexer
from engine.db.query import QueryEngine
from engine.db.schema import init_schema


def _index(conn: sqlite3.Connection, url: str, title: str) -> None:
    # Populate through the indexer rather than raw INSERTs, the way
    # test_query.py does: the pages schema carries NOT NULL columns a
    # hand-written insert has to keep in step with.
    Indexer(conn).index({"url": url, "title": title, "extracted": {"text_preview": "x"}}, "r")


@pytest.fixture()
def qe() -> QueryEngine:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    _index(conn, "https://a.test/", "A")
    _index(conn, "https://b.test/", "B")
    conn.commit()
    return QueryEngine(conn)


def _count(qe: QueryEngine) -> int:
    return int(qe.select("SELECT COUNT(*) AS n FROM pages")[0]["n"])


# --- the statement that started it -----------------------------------------


def test_a_with_prefixed_delete_is_refused(qe: QueryEngine) -> None:
    before = _count(qe)
    with pytest.raises(ValueError):
        qe.select("WITH x AS (SELECT 1) DELETE FROM pages")
    assert _count(qe) == before, "rows were deleted through a SELECT-only tool"


def test_a_with_prefixed_delete_removes_nothing_even_when_scoped(qe: QueryEngine) -> None:
    # The exact statement used against the deployment, minus the WHERE that
    # made it safe there.
    with pytest.raises(ValueError):
        qe.select("WITH x AS (SELECT 1) DELETE FROM pages WHERE url = 'https://a.test/'")
    assert _count(qe) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) INSERT INTO pages (url, domain, first_seen) VALUES ('https://c.test/', 'c.test', 1)",
        "WITH x AS (SELECT 1) UPDATE pages SET title = 'hacked'",
        "WITH x AS (SELECT 1) DELETE FROM pages",
    ],
)
def test_every_writing_statement_a_with_can_precede_is_refused(qe: QueryEngine, sql: str) -> None:
    with pytest.raises(ValueError):
        qe.select(sql)
    rows = qe.select("SELECT url, title FROM pages ORDER BY url")
    assert len(rows) == 2
    assert all(r["title"] != "hacked" for r in rows)


def test_the_refusal_says_what_was_wrong(qe: QueryEngine) -> None:
    with pytest.raises(ValueError, match="read-only"):
        qe.select("WITH x AS (SELECT 1) DELETE FROM pages")


# --- and the prefix check still catches the plain cases early ---------------


@pytest.mark.parametrize("sql", ["DELETE FROM pages", "DROP TABLE pages", "PRAGMA table_info(pages)"])
def test_a_bare_write_is_still_refused_by_the_first_word(qe: QueryEngine, sql: str) -> None:
    with pytest.raises(ValueError):
        qe.select(sql)
    assert _count(qe) == 2


# --- reads must be untouched ------------------------------------------------


def test_a_plain_select_still_works(qe: QueryEngine) -> None:
    assert _count(qe) == 2


def test_a_read_only_cte_still_works(qe: QueryEngine) -> None:
    # `WITH` is not the problem and must keep working.
    rows = qe.select("WITH x AS (SELECT url FROM pages) SELECT COUNT(*) AS n FROM x")
    assert rows[0]["n"] == 2


def test_a_recursive_cte_still_works(qe: QueryEngine) -> None:
    rows = qe.select(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT COUNT(*) AS n FROM n"
    )
    assert rows[0]["n"] == 5


def test_a_join_and_an_aggregate_still_work(qe: QueryEngine) -> None:
    rows = qe.select("SELECT p.url, COUNT(*) AS n FROM pages p GROUP BY p.url ORDER BY p.url")
    assert [r["url"] for r in rows] == ["https://a.test/", "https://b.test/"]


def test_the_authorizer_is_removed_afterwards(qe: QueryEngine) -> None:
    # It is installed on the connection the indexer also writes through, so
    # leaving it behind would break every later write.
    qe.select("SELECT 1 AS n")
    _index(qe._conn, "https://c.test/", "C")
    qe._conn.commit()
    assert _count(qe) == 3


def test_it_is_removed_after_a_refusal_too(qe: QueryEngine) -> None:
    with pytest.raises(ValueError):
        qe.select("WITH x AS (SELECT 1) DELETE FROM pages")
    _index(qe._conn, "https://c.test/", "C")
    qe._conn.commit()
    assert _count(qe) == 3
