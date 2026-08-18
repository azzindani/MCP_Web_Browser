"""Tests for the shared output directory and public_url on exports."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.exchange import public_url_for
from shared.path_safety import UnsafePathError, resolve_path


def test_data_root_falls_back_to_shared_output_dir(monkeypatch, tmp_path):
    # Inside a container cwd is /app, which nothing outside can read — an
    # export landing there is invisible to the caller that asked for it.
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    assert resolve_path("pages.csv") == tmp_path / "pages.csv"


def test_explicit_data_root_still_wins(monkeypatch, tmp_path):
    root = tmp_path / "explicit"
    root.mkdir()
    monkeypatch.setenv("MCP_DATA_ROOT", str(root))
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path / "shared"))
    assert resolve_path("pages.csv") == root / "pages.csv"


def test_traversal_out_of_the_shared_dir_is_still_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(UnsafePathError):
        resolve_path("../escaped.csv")


def test_public_url_for_an_export(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://files.example.test/data")
    target = tmp_path / "pages.csv"
    target.write_text("url,title\n")
    assert public_url_for(target) == "https://files.example.test/data/pages.csv"


def test_public_url_empty_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    assert public_url_for(tmp_path / "pages.csv") == ""


def test_cwd_still_used_when_nothing_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.delenv("MCP_OUTPUT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_path("pages.csv") == Path(tmp_path).resolve() / "pages.csv"
