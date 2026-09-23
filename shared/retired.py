"""A tool name that keeps answering after it leaves tools/list.

Every name in `tools/list` costs a model attention on every turn. Two cases
drop a name from the list without breaking anyone who learned it:

- A duplicate whose successor does the job. MCP_Data_Analyst's medium tier
  served `extended_stats` (the statistics server's own function),
  `statistical_tests` (a subset of `statistical_test`), `filter_rows` (what
  `filter_dataset` was upgraded from) and `compute_aggregations`
  (`aggregate_dataset` in groupby mode). Each still answers, and its answer
  names the tool to use (`note=True`, the default).
- A tool now reached as an `action` of a domain tool on the same server
  (shared/domain_tools.py). It still answers under its own name, unchanged
  and without a note (`note=False`): the domain tool calls the same tool, and
  a note there would tell the caller to use what it is already using.

The tool stays registered and declared in source; only the listing changes.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any


def retire(mcp: Any, successors: dict[str, str], note: bool = True) -> None:
    """Drop each name in `successors` from tools/list; it still answers.

    `successors` maps a retired name to what replaces it, e.g.
    {"statistical_tests": "statistical_test on the statistics server"}. With
    `note`, each answer carries `retired` naming it. Raises KeyError for a name
    this server does not register, so a typo cannot silently retire nothing.
    """
    manager = mcp._tool_manager
    missing = sorted(name for name in successors if name not in manager._tools)
    if missing:
        raise KeyError(f"cannot retire what is not registered: {missing}")
    listing = manager.list_tools
    retired: set[str] = getattr(listing, "__retired__", set())
    if not hasattr(listing, "__retired__"):

        def list_tools() -> list[Any]:
            return [tool for tool in listing() if tool.name not in retired]

        list_tools.__retired__ = retired  # type: ignore[attr-defined]
        manager.list_tools = list_tools
    retired.update(successors)
    if not note:
        return
    for name, successor in successors.items():
        tool = manager._tools[name]
        tool.fn = _naming_successor(tool.fn, name, successor)


def _naming_successor(fn: Any, name: str, successor: str) -> Any:
    text = f"{name} is no longer listed; use {successor}, which does this job."
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def answering_async(*a: Any, **kw: Any) -> Any:
            result = await fn(*a, **kw)
            if isinstance(result, dict):
                result["retired"] = text
            return result

        return answering_async

    @functools.wraps(fn)
    def answering(*a: Any, **kw: Any) -> Any:
        result = fn(*a, **kw)
        if isinstance(result, dict):
            result["retired"] = text
        return result

    return answering
