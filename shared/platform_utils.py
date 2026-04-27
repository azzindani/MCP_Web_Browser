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
