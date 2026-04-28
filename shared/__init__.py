"""Cross-cutting helpers used by both engine/** and server.py.

No imports from `mcp.*` are allowed in this package.
"""
from shared.handover import make_handover, next_step
from shared.progress import fail, info, ok, undo, warn
from shared.receipt import append_receipt

__all__ = [
    "ok", "fail", "info", "warn", "undo",
    "append_receipt",
    "make_handover", "next_step",
]
