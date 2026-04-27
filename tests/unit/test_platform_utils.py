"""Verify hardware-mode helpers read MCP_CONSTRAINED_MODE at call time."""

from __future__ import annotations

import pytest

from shared import platform_utils as pu


def test_default_is_unconstrained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_CONSTRAINED_MODE", raising=False)
    assert pu.is_constrained_mode() is False
    assert pu.get_max_rows() == 100
    assert pu.get_max_results() == 50
    assert pu.get_max_depth() == 5
    assert pu.get_max_pages() == 250
    assert pu.get_inspect_chars() == 2_000


def test_constrained_mode_shrinks_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "1")
    assert pu.is_constrained_mode() is True
    assert pu.get_max_rows() == 20
    assert pu.get_max_results() == 10
    assert pu.get_max_depth() == 3
    assert pu.get_max_pages() == 25
    assert pu.get_inspect_chars() == 500


def test_env_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flipping the env after import must take effect without a reload."""
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "0")
    assert pu.get_max_rows() == 100
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "1")
    assert pu.get_max_rows() == 20
    monkeypatch.setenv("MCP_CONSTRAINED_MODE", "0")
    assert pu.get_max_rows() == 100
