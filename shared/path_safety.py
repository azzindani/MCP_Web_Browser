"""Resolve user-supplied paths to a safe absolute Path.

`resolve_path` rejects traversal attempts (`..`), null bytes, and any
target outside the configured root. The root defaults to the current
working directory but can be overridden with `MCP_DATA_ROOT`.
"""

from __future__ import annotations

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path escapes the data root or contains forbidden bytes."""


def _data_root() -> Path:
    raw = os.environ.get("MCP_DATA_ROOT") or os.getcwd()
    return Path(raw).expanduser().resolve(strict=False)


def resolve_path(p: str | os.PathLike[str]) -> Path:
    raw = os.fspath(p)
    if "\x00" in raw:
        raise UnsafePathError("null byte in path")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = _data_root() / candidate
    resolved = candidate.resolve(strict=False)

    root = _data_root()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"{resolved} escapes data root {root}") from exc

    return resolved
