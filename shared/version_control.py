"""Snapshot-before-write and atomic-rename helpers.

Every tool that mutates persistent state calls `snapshot()` first. Files
are written via `atomic_write*` so a crash never leaves a half-written
file at the canonical name.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from shared.path_safety import resolve_path


def snapshot(path: str | os.PathLike[str]) -> Path | None:
    """Copy `path` to `path.bak` if it exists. Returns the backup path or None.

    Caller may ignore the return value; the side effect is what matters.
    """
    target = resolve_path(path)
    if not target.exists():
        return None
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    return backup


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # fsync the directory so the rename is durable across crashes.
    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return target


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    encoding: str = "utf-8",
) -> Path:
    return atomic_write_bytes(path, text.encode(encoding))
