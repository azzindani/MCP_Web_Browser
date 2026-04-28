"""Shared pytest fixtures. Sets MCP_DATA_ROOT so path_safety allows tmp_path."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_tmp_path(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", "/")
