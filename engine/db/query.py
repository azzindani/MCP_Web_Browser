"""Read-only query helpers. All public methods cap their result sets.

`search()` and `select()` return at most `get_max_rows()` rows when no
explicit `limit` is given. Table names for FTS search are whitelisted
to prevent SQL injection — values always go through parameter binding.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Final

from shared.platform_utils import get_max_rows

FTS_TABLES: Final[frozenset[str]] = frozenset({"fts_pages", "fts_news", "fts_files"})

STATS_TABLES: Final[tuple[str, ...]] = (
    "pages",
    "stocks",
    "news",
    "market_indices",
    "files",
    "links",
    "domains",
    "endpoints",
    "task_log",
    "runs",
)


class QueryEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def search(
        self,
        query: str,
        table: str = "fts_pages",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if table not in FTS_TABLES:
            raise ValueError(f"unknown fts table: {table!r}")
        cap = limit if limit is not None else get_max_rows()
        # Table name is validated against the whitelist above.
        sql = f"SELECT * FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?"  # noqa: S608
        rows = self._conn.execute(sql, (query, cap)).fetchall()
        return [dict(r) for r in rows]

    def select(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run a parameterised SELECT. Caller must pass a SELECT-only statement.

        The engine never builds SQL by string interpolation; values come
        through `params` and are bound as positional placeholders.
        """
        stripped = sql.lstrip().lower()
        if not stripped.startswith("select") and not stripped.startswith("with"):
            raise ValueError("select() only accepts SELECT/WITH statements")
        cap = limit if limit is not None else get_max_rows()
        cur = self._conn.execute(sql, params)
        rows = cur.fetchmany(cap)
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in STATS_TABLES:
            try:
                row = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608
                ).fetchone()
                out[table] = int(row["n"]) if row else 0
            except sqlite3.OperationalError:
                out[table] = 0
        size_row = self._conn.execute(
            "SELECT page_count * page_size AS s FROM pragma_page_count(), pragma_page_size()"
        ).fetchone()
        out["db_bytes"] = int(size_row["s"]) if size_row and size_row["s"] else 0
        return out

    def to_csv(self, table: str, limit: int = 100_000) -> str:
        # Whitelist using the schema's known tables.
        if table not in STATS_TABLES:
            raise ValueError(f"unknown table: {table!r}")
        cur = self._conn.execute(
            f"SELECT * FROM {table} LIMIT ?",  # noqa: S608
            (limit,),
        )
        rows = cur.fetchall()
        if not rows:
            return ""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(rows[0].keys())
        writer.writerows(tuple(r) for r in rows)
        return buf.getvalue()
