"""Resolve user-supplied paths to a safe absolute Path.

`resolve_path` rejects traversal attempts (`..`), null bytes, and any
target outside the configured root. The root defaults to `MCP_DATA_ROOT`,
else the current working directory — that is where the crawl DB lives, so it
must not move. A caller that writes somewhere else on purpose (query_export
into the shared output directory) passes an explicit `root`.
"""

from __future__ import annotations

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a path escapes the data root or contains forbidden bytes."""


def _data_root() -> Path:
    """Return the default root: MCP_DATA_ROOT, else the working directory."""
    raw = os.environ.get("MCP_DATA_ROOT") or os.getcwd()
    return Path(raw).expanduser().resolve(strict=False)


def export_root() -> Path:
    """Return the root for files a caller asked this server to hand back.

    MCP_OUTPUT_DIR is the shared directory every sibling MCP server writes
    into; unset, exports stay beside everything else. This is deliberately
    separate from _data_root(): pointing the *default* root at the shared
    directory would move the crawl DB there too, since it resolves through
    the same function.
    """
    raw = os.environ.get("MCP_OUTPUT_DIR")
    return Path(raw).expanduser().resolve(strict=False) if raw else _data_root()


def resolve_path(p: str | os.PathLike[str], root: Path | None = None) -> Path:
    raw = os.fspath(p)
    if "\x00" in raw:
        raise UnsafePathError("null byte in path")

    root = root or _data_root()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"{resolved} escapes data root {root}") from exc

    return resolved
