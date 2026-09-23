"""Every browser tool as a domain tool's action: two names in tools/list, not thirteen.

A model reads every tool name on every turn. This server now lists `browse`
(the live web) and `query` (the local store) -- `crawl` too when that tier is
on -- and each `action` is one of the original tools by its own name, run by
that tool itself. The originals leave the list and keep answering under
their own names, so no client that already calls them breaks.
"""

from __future__ import annotations

import asyncio

import pytest

import server


def _listed() -> dict:
    return {t.name: t for t in asyncio.run(server.app.list_tools())}


def _call(tool: str, action: str, args: dict) -> dict:
    return asyncio.run(server.app._tool_manager._tools[tool].run({"action": action, "args": args}))


ORIGINALS = sorted(n for n in server.app._tool_manager._tools if n not in server.DOMAINS)


class TestTwoNotThirteen:
    def test_only_the_domain_tools_are_listed(self):
        assert sorted(_listed()) == sorted(server.DOMAINS)
        assert set(server.DOMAINS) <= {"browse", "query", "crawl"}

    def test_every_original_is_exactly_one_action(self):
        actions = [a for t in _listed().values() for a in t.inputSchema["properties"]["action"]["enum"]]
        assert sorted(actions) == ORIGINALS
        assert len(actions) == len(set(actions))

    def test_an_original_still_answers_under_its_own_name(self):
        assert "browse_datetime" in server.app._tool_manager._tools
        result = asyncio.run(server.app._tool_manager.call_tool("browse_datetime", {}))
        assert result.get("ok") is True and "retired" not in result


class TestAnActionIsTheOriginal:
    def test_it_answers_as_the_original_does(self):
        direct = asyncio.run(server.app._tool_manager.call_tool("browse_datetime", {}))
        via = _call("browse", "browse_datetime", {})
        assert via["ok"] is True and via["success"] is True
        assert set(via) == set(direct)


class TestARefusalSpeaksThisServersEnvelope:
    @pytest.mark.parametrize(
        ("tool", "action", "args", "says"),
        [
            ("query", "browse_fetch", {}, "browse"),
            ("browse", "no_such_thing", {}, "browse_search"),
            ("browse", "browse_datetime", {"colour": "red"}, "colour"),
        ],
    )
    def test_ok_and_success_are_both_false(self, tool, action, args, says):
        result = _call(tool, action, args)
        assert result["ok"] is False and result["success"] is False
        assert says in result["error"] + result["hint"]
