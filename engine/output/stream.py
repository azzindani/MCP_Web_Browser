"""JSONL stream writer. Every result written immediately — never batched.

Mirrors output/stream.ts in krawl. Appends to an existing file so
multiple runs accumulate in one stream. Line-buffered so data survives
a SIGKILL between writes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.path_safety import resolve_path


class StreamWriter:
    def __init__(self, file_path: str | Path = "krawl_output.jsonl") -> None:
        self._path = resolve_path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1 = line-buffered: each write() flushes at the newline.
        self._handle = self._path.open("a", encoding="utf-8", buffering=1)
        self._count = 0

    def write(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, default=str) + "\n")
        self._count += 1

    def close(self) -> None:
        try:
            self._handle.flush()
            self._handle.close()
        except OSError:
            pass

    def __enter__(self) -> "StreamWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
        return records
