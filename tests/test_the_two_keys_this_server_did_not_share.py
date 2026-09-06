"""Twenty-five endpoints answer `success`. This one answered `ok`.

Round 28 ran every tool on every endpoint in the fleet and classified the
response envelope. Seventeen of 338 responses fell outside the fleet contract,
and they came from exactly two causes:

* thirteen were this server, on all thirteen of its tools, reporting the outcome
  under `ok` where the other twenty-five endpoints report it under `success`;
* four were argument TYPE errors escaping as raw pydantic dumps -- on browser,
  docs-read, docs-edit and math, which are precisely the four repos that never
  received `shared/arg_errors.py`.

Both are one key a caller branches on. A client written against the fleet
contract reads `success` off a browser response, gets `None`, and cannot tell a
refusal from a result; and a wrong-typed argument produced no `success` at all,
plus a pydantic.dev URL from a server whose founding constraint is that nothing
leaves the machine.

`ok` is not removed. Renaming it would fix the contract and break every existing
caller and every one of this repo's own tests -- the trade `arg_alias` and
`value_alias` already refused twice. Both keys go out, and they always agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.envelope import with_success  # noqa: E402


class TestBothKeysGoOut:
    def test_success_mirrors_ok(self):
        assert with_success({"ok": True, "op": "x"})["success"] is True
        assert with_success({"ok": False, "op": "x"})["success"] is False

    def test_ok_survives(self):
        out = with_success({"ok": False, "op": "x"})
        assert out["ok"] is False

    def test_success_sits_next_to_ok_not_at_the_end(self):
        """A truncated response should carry the verdict in its first lines."""
        out = with_success({"ok": True, "op": "x", "payload": "..."})
        assert list(out)[:2] == ["ok", "success"]

    def test_a_payload_that_already_has_success_is_left_alone(self):
        out = with_success({"ok": True, "success": False})
        assert out["success"] is False

    def test_a_payload_with_no_ok_is_untouched(self):
        payload = {"result": 1}
        assert with_success(payload) is payload

    def test_a_non_dict_is_untouched(self):
        assert with_success("text") == "text"
        assert with_success(None) is None


class TestEveryToolCarriesIt:
    @pytest.mark.parametrize(
        "tool,args",
        [
            ("query_locate", {}),
            ("query_stats", {}),
        ],
    )
    def test_the_offline_tools_answer_with_both(self, tool, args):
        """Chosen because they touch no network: the envelope is the subject here."""
        import engine

        payload = with_success(getattr(engine, tool)(**args))
        assert payload["success"] == payload["ok"]

    def test_the_wrapper_is_installed_on_the_live_app(self):
        import server

        wrapped = [
            name
            for name, tool in server.app._tool_manager._tools.items()
            if getattr(getattr(tool, "fn", None), "__envelope_wrapped__", False)
        ]
        assert len(wrapped) == len(server.app._tool_manager._tools), sorted(
            set(server.app._tool_manager._tools) - set(wrapped)
        )


class TestATypeErrorStaysInsideTheContract:
    def test_a_wrong_type_comes_back_in_the_fleet_shape(self):
        """Behavioural, not by marker: enforce_known_arguments wraps this one
        afterwards, so the attribute lives on an inner closure and only the
        answer tells you the chain is installed."""
        import asyncio
        import json

        import server

        out = asyncio.run(server.app._tool_manager.call_tool("browse_search", {"query": 123}))
        text = out if isinstance(out, str) else json.dumps(_as_json(out))
        assert "success" in text, text[:300]
        assert "pydantic.dev" not in text, "an offline server sent the caller to the internet"

    def test_a_pydantic_message_becomes_the_fleet_shape(self):
        from shared.arg_errors import _refusal

        message = (
            "1 validation error for browse_searchArguments\nquery\n"
            "  Input should be a valid string [type=string_type, input_value=123, input_type=int]\n"
            "    For further information visit https://errors.pydantic.dev/2.13/v/string_type"
        )
        out = _refusal("browse_search", ["limit", "query"], message)
        assert out["success"] is False
        assert "query" in out["error"]
        assert "pydantic.dev" not in out["error"], "an offline server sent the caller to the internet"
        assert out["hint"]
        assert out["token_estimate"] > 0

    def test_a_bracket_in_the_input_value_no_longer_defeats_the_parser(self):
        """The regex could not cross a `]` inside input_value, so nested data fell through.

        This is what made two Office tools answer a missing argument with the
        raw dump while a third answered cleanly: the difference was whether the
        rejected call happened to contain a list.
        """
        from shared.arg_errors import explain

        message = (
            "1 validation error for add_tableArguments\nafter_paragraph_index\n"
            "  Field required [type=missing, input_value={'file_path': '/w...['a', 'b'], ['c', 'd']]}, "
            "input_type=dict]"
        )
        assert explain(message) == [("after_paragraph_index", "Field required")]


def _as_json(result):
    """Both FastMCP flavours, reduced to something a test can read."""
    for attr in ("structured_content", "content"):
        value = getattr(result, attr, None)
        if value is not None:
            return value if isinstance(value, dict) else [getattr(c, "text", str(c)) for c in value]
    return result
