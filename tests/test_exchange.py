"""Tests for the export root and public_url on query_export.

The crawl DB resolves through the same resolve_path() as everything else, so
the shared output directory must be a *separate* root used only by exports —
pointing the default root at it moves the DB to a fresh empty file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.exchange import public_url_for
from shared.path_safety import UnsafePathError, export_root, resolve_path


def test_export_root_is_the_shared_output_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    assert export_root() == tmp_path
    assert resolve_path("pages.csv", root=export_root()) == tmp_path / "pages.csv"


def test_shared_output_dir_does_not_move_the_default_root(monkeypatch, tmp_path):
    """The regression this separation exists to prevent.

    krawl.db resolves through the default root. If MCP_OUTPUT_DIR fed into it,
    the DB would relocate to the shared directory and come back empty.
    """
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path / "shared"))
    monkeypatch.chdir(tmp_path)
    assert resolve_path("krawl.db") == Path(os.getcwd()).resolve() / "krawl.db"


def test_export_root_falls_back_to_the_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    assert export_root() == tmp_path


def test_explicit_data_root_still_governs_the_db(monkeypatch, tmp_path):
    root = tmp_path / "explicit"
    root.mkdir()
    monkeypatch.setenv("MCP_DATA_ROOT", str(root))
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path / "shared"))
    assert resolve_path("krawl.db") == root / "krawl.db"


def test_traversal_out_of_the_export_root_is_still_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DATA_ROOT", raising=False)
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(UnsafePathError):
        resolve_path("../escaped.csv", root=export_root())


def test_traversal_out_of_the_data_root_is_still_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    with pytest.raises(UnsafePathError):
        resolve_path("../escaped.db")


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


def test_export_helpers_accept_the_export_root(monkeypatch, tmp_path):
    """snapshot/atomic_write re-validate the path, so they need the same root.

    Without threading it through, an export into the shared directory is
    resolved successfully and then rejected one line later by the snapshot.
    """
    from shared.version_control import atomic_write_text, snapshot

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path / "app"))
    (tmp_path / "app").mkdir()
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(shared))

    target = resolve_path("pages.csv", root=export_root())
    assert snapshot(target, root=export_root()) is None
    written = atomic_write_text(target, "url,title\n", root=export_root())
    assert written.read_text() == "url,title\n"
    assert written.stat().st_mode & 0o044 == 0o044
    assert snapshot(target, root=export_root()) is not None
