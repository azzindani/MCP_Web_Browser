"""CSV / JSON export. Wraps QueryEngine for table-level exports.

Mirrors output/export.ts in krawl. snapshot() is called before every
write; files land via atomic_write_text so a crash leaves the previous
version intact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from shared.path_safety import resolve_path
from shared.version_control import atomic_write_text, snapshot

if TYPE_CHECKING:
    from engine.db.query import QueryEngine

_EXPORT_TABLES = (
    "pages",
    "stocks",
    "news",
    "market_indices",
    "files",
    "links",
    "domains",
    "endpoints",
)


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Exporter:
    def __init__(self, query: QueryEngine) -> None:
        self._query = query

    def export_csv(
        self,
        table: str,
        out_dir: str | Path,
        limit: int = 100_000,
    ) -> str:
        out = resolve_path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        csv_text = self._query.to_csv(table, limit=limit)
        if not csv_text:
            return ""
        out_path = out / f"{table}.csv"
        snapshot(out_path)
        atomic_write_text(out_path, csv_text)
        return str(out_path)

    def export_all_csv(self, out_dir: str | Path) -> list[str]:
        paths: list[str] = []
        for table in _EXPORT_TABLES:
            try:
                p = self.export_csv(table, out_dir)
                if p:
                    _stderr(f"exported {table} → {p}")
                    paths.append(p)
            except Exception as exc:
                _stderr(f"failed to export {table}: {exc}")
        return paths

    def export_json(self, table: str, out_dir: str | Path) -> str:
        out = resolve_path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        rows = self._query.select(f"SELECT * FROM {table}", limit=100_000)  # noqa: S608
        text = json.dumps(rows, indent=2, default=str)
        out_path = out / f"{table}.json"
        snapshot(out_path)
        atomic_write_text(out_path, text)
        return str(out_path)
