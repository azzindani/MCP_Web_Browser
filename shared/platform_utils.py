"""Hardware-mode helpers. All limits are read at CALL time, never module-import time.

`MCP_CONSTRAINED_MODE=1` shrinks every cap so an 8 GB-VRAM / 9B-Q4 host can
run the server without overflowing its context window. Tests flip the env
var with monkeypatch and expect the changes to take effect immediately.
"""

from __future__ import annotations

import os


def is_constrained_mode() -> bool:
    return os.environ.get("MCP_CONSTRAINED_MODE", "0") == "1"


def get_max_rows() -> int:
    return 20 if is_constrained_mode() else 100


def get_max_results() -> int:
    return 10 if is_constrained_mode() else 50


def get_max_depth() -> int:
    return 3 if is_constrained_mode() else 5


def get_max_pages() -> int:
    return 25 if is_constrained_mode() else 250


def get_inspect_chars() -> int:
    return 500 if is_constrained_mode() else 2_000


def get_research_depth() -> int:
    """Default depth for browse_research. Override with MCP_RESEARCH_DEPTH."""
    raw = os.environ.get("MCP_RESEARCH_DEPTH", "")
    if raw.isdigit():
        return max(1, min(3, int(raw)))
    return 2


def get_research_fetch_top() -> int:
    """Default fetch_top for browse_research. Override with MCP_RESEARCH_FETCH_TOP."""
    raw = os.environ.get("MCP_RESEARCH_FETCH_TOP", "")
    if raw.isdigit():
        return max(0, min(20, int(raw)))
    return 5


def get_research_breadth() -> int:
    """Default breadth for browse_research. Override with MCP_RESEARCH_BREADTH."""
    raw = os.environ.get("MCP_RESEARCH_BREADTH", "")
    if raw.isdigit():
        return max(1, min(3, int(raw)))
    return 1


def get_search_limit() -> int:
    """Default limit for browse_search. Override with MCP_SEARCH_LIMIT."""
    raw = os.environ.get("MCP_SEARCH_LIMIT", "")
    if raw.isdigit():
        return max(1, min(50, int(raw)))
    return 10 if is_constrained_mode() else 10
