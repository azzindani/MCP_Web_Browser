"""Verify Checkpoint atomic save + resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.core.checkpoint import Checkpoint


def test_fresh_checkpoint_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    cp = Checkpoint("ckpt.json", run_id="r-1")
    assert cp.run_id() == "r-1"
    assert cp.done_urls() == set()


def test_save_and_reload_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    cp = Checkpoint("ckpt.json", run_id="r-2")
    cp.save({"https://a.example/", "https://b.example/"}, total_count=2)

    saved = json.loads((tmp_path / "ckpt.json").read_text())
    assert saved["run_id"] == "r-2"
    assert saved["done_count"] == 2

    cp2 = Checkpoint("ckpt.json", run_id="r-2")
    assert cp2.done_urls() == {"https://a.example/", "https://b.example/"}


def test_corrupt_file_starts_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    (tmp_path / "ckpt.json").write_text("{not json")
    cp = Checkpoint("ckpt.json", run_id="r-3")
    assert cp.done_urls() == set()
    assert cp.run_id() == "r-3"


def test_clear_removes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    cp = Checkpoint("ckpt.json", run_id="r-4")
    cp.save({"https://x.example/"}, total_count=1)
    assert (tmp_path / "ckpt.json").exists()
    cp.clear()
    assert not (tmp_path / "ckpt.json").exists()
    assert cp.done_urls() == set()


def test_save_is_atomic_no_tmp_leftover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_DATA_ROOT", str(tmp_path))
    cp = Checkpoint("ckpt.json", run_id="r-5")
    cp.save({"https://a"}, 1)
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == []
