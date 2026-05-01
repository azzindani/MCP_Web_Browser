"""Terminal progress helpers. All output goes to stderr only.

Never writes to stdout — that stream is reserved for MCP stdio framing.
DisplaySink is injectable so callers can be tested without patching sys.stderr.
"""

from __future__ import annotations

import sys
from typing import IO


class DisplaySink:
    """Write progress lines to stderr (default) or any writable sink."""

    def __init__(self, sink: IO[str] | None = None) -> None:
        self._sink = sink or sys.stderr

    def line(self, msg: str) -> None:
        self._sink.write(msg + "\n")
        self._sink.flush()

    def section(self, title: str) -> None:
        self.line("=" * 60)
        self.line(title)
        self.line("=" * 60)

    def ok(self, label: str, detail: str = "") -> None:
        self.line(f"  ✓ {label}" + (f"  {detail}" if detail else ""))

    def err(self, label: str, detail: str = "") -> None:
        self.line(f"  ✗ {label}" + (f"  {detail}" if detail else ""))

    def info(self, msg: str) -> None:
        self.line(f"  {msg}")


_default = DisplaySink()


def line(msg: str) -> None:
    _default.line(msg)


def section(title: str) -> None:
    _default.section(title)


def ok(label: str, detail: str = "") -> None:
    _default.ok(label, detail)


def err(label: str, detail: str = "") -> None:
    _default.err(label, detail)


def info(msg: str) -> None:
    _default.info(msg)
