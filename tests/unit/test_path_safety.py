"""Verify resolve_path() rejects traversal and null bytes."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.path_safety import UnsafePathError, resolve_path


def test_relative_path_resolves_under_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    out = resolve_path("foo/bar.db")
    assert out == (tmp_path / "foo" / "bar.db").resolve()


def test_traversal_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    with pytest.raises(UnsafePathError):
        resolve_path("../escape.db")


def test_null_byte_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    with pytest.raises(UnsafePathError):
        resolve_path("foo\x00bar.db")


def test_absolute_path_outside_root_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    with pytest.raises(UnsafePathError):
        resolve_path(str(tmp_path / "elsewhere" / "x.db"))
