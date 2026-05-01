"""Verify snapshot() and atomic_write_*()."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import version_control as vc


def test_snapshot_returns_none_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    assert vc.snapshot("never_existed.db") is None


def test_snapshot_creates_bak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    target = tmp_path / "data.db"
    target.write_bytes(b"original")
    backup = vc.snapshot("data.db")
    assert backup is not None
    assert backup.read_bytes() == b"original"
    assert backup.name == "data.db.bak"


def test_atomic_write_text_replaces_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    out = vc.atomic_write_text("nested/dir/out.json", '{"k": 1}')
    assert out.read_text() == '{"k": 1}'
    # No leftover .tmp file in the parent directory.
    leftovers = [p.name for p in out.parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_atomic_write_overwrites_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    vc.atomic_write_bytes("file.bin", b"first")
    vc.atomic_write_bytes("file.bin", b"second")
    assert (tmp_path / "file.bin").read_bytes() == b"second"
