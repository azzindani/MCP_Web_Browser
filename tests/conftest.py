"""Shared pytest fixtures. Sets MCP_DATA_ROOT to per-test tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _allow_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
