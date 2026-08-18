"""Resolve user-supplied paths to a safe absolute Path.

`resolve_path` rejects traversal attempts (`..`), null bytes, and any
target outside the configured root. The root is `MCP_DATA_ROOT`, else the
shared output directory `MCP_OUTPUT_DIR` this server's siblings write into,
else the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path escapes the data root or contains forbidden bytes."""


def _data_root() -> Path:
    """Return the only directory paths may resolve inside.

    Falls back to MCP_OUTPUT_DIR before cwd so an exported table lands in the
    same shared directory every sibling MCP server writes to — inside a
    container, cwd is /app, which nothing outside the container can read.
    """
    raw = os.environ.get("MCP_DATA_ROOT") or os.environ.get("MCP_OUTPUT_DIR") or os.getcwd()
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
