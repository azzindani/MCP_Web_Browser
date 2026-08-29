"""Save and restore crawl-run state. Survives crashes, restarts, kill -9.

Persists the set of URLs already processed so a resumed run skips them.
Writes are atomic via `shared.version_control.atomic_write_text`, so a
mid-write crash never leaves the canonical file partially populated.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.path_safety import resolve_path
from shared.version_control import atomic_write_text


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stderr(msg: str) -> None:
    """Engine-internal log. Never write to stdout (corrupts MCP stdio)."""
    print(msg, file=sys.stderr, flush=True)


@dataclass
class CheckpointData:
    run_id: str
    saved_at: str
    done_urls: list[str] = field(default_factory=list)
    done_count: int = 0
    total_count: int = 0
    config: dict[str, Any] = field(default_factory=dict)


class Checkpoint:
    def __init__(
        self,
        file_path: str | os.PathLike[str],
        run_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._path: Path = resolve_path(file_path)
        self._data = self._load(run_id, config or {})

    def _load(self, run_id: str, config: dict[str, Any]) -> CheckpointData:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                _stderr(f"checkpoint loaded: {raw.get('done_count', 0)} done (run {raw.get('run_id')})")
                return CheckpointData(
                    run_id=str(raw.get("run_id") or run_id),
                    saved_at=str(raw.get("saved_at") or _now_iso()),
                    done_urls=list(raw.get("done_urls") or []),
                    done_count=int(raw.get("done_count") or 0),
                    total_count=int(raw.get("total_count") or 0),
                    config=dict(raw.get("config") or {}),
                )
            except json.JSONDecodeError, ValueError, TypeError:
                _stderr("checkpoint corrupt — starting fresh")

        return CheckpointData(run_id=run_id, saved_at=_now_iso(), config=dict(config))

    def save(self, done_urls: set[str], total_count: int) -> None:
        self._data.saved_at = _now_iso()
        self._data.done_urls = sorted(done_urls)
        self._data.done_count = len(done_urls)
        self._data.total_count = total_count
        atomic_write_text(
            self._path,
            json.dumps(
                {
                    "run_id": self._data.run_id,
                    "saved_at": self._data.saved_at,
                    "done_urls": self._data.done_urls,
                    "done_count": self._data.done_count,
                    "total_count": self._data.total_count,
                    "config": self._data.config,
                },
                indent=2,
            ),
        )

    def done_urls(self) -> set[str]:
        return set(self._data.done_urls)

    def run_id(self) -> str:
        return self._data.run_id

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._data.done_urls = []
        self._data.done_count = 0
