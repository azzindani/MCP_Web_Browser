"""Snapshot-before-write and atomic-rename helpers.

Every tool that mutates persistent state calls `snapshot()` first. Files
are written via `atomic_write*` so a crash never leaves a half-written
file at the canonical name.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from shared.path_safety import resolve_path


def _umask_default_mode() -> int:
    """Return the mode a plain open() would create: 0o666 masked by the umask.

    Read once at import, while the process is still single-threaded — reading
    a umask requires setting it, which is process-wide.
    """
    current = os.umask(0o022)
    os.umask(current)
    return 0o666 & ~current


_DEFAULT_FILE_MODE = _umask_default_mode()


def snapshot(path: str | os.PathLike[str], root: Path | None = None) -> Path | None:
    """Copy `path` to `path.bak` if it exists. Returns the backup path or None.

    Caller may ignore the return value; the side effect is what matters.
    `root` overrides the path guard's default root for callers writing
    somewhere other than the data root (query_export -> export_root()).
    """
    target = resolve_path(path, root=root)
    if not target.exists():
        return None
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    return backup


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, root: Path | None = None) -> Path:
    target = resolve_path(path, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600 and os.replace keeps it — wrong for anything
        # landing in a shared directory the file server also has to read.
        os.chmod(tmp, _DEFAULT_FILE_MODE)
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # fsync the directory so the rename is durable across crashes.
    # Windows does not support opening directories with os.open(); NTFS
    # provides equivalent guarantees without an explicit dir fsync.
    if sys.platform != "win32":
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
    root: Path | None = None,
) -> Path:
    return atomic_write_bytes(path, text.encode(encoding), root=root)
