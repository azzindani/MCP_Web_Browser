"""Workflow handover helpers — guide the LLM to the next logical tool call."""

from __future__ import annotations

from typing import Any


def next_step(tool: str, reason: str) -> dict[str, str]:
    """Build a single suggested-next entry."""
    return {"tool": tool, "reason": reason}


def make_handover(
    suggested_next: list[dict[str, str]] | None = None,
    carry_forward: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a handover block to embed in a tool response."""
    result: dict[str, Any] = {}
    if suggested_next:
        result["suggested_next"] = suggested_next
    if carry_forward:
        result["carry_forward"] = carry_forward
    return result
