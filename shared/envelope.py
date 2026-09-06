"""Answer with the key the rest of the fleet answers with.

Twenty-five of the fleet's twenty-six endpoints report the outcome of a call as
`success`. This one reports it as `ok`, on all thirteen tools:

    {"ok": false, "op": "browse_extract", "error": "...", "hint": "..."}

Everything else about the envelope already matches -- `op`, `error`, `hint`,
`progress`, `token_estimate`. It is one key, and it is the one key a caller
branches on, so a client written against the fleet contract reads `success` off
a browser response, gets `None`, and cannot tell a refusal from a result. Round
28 found it by running every tool on every endpoint and classifying the
envelope: thirteen of the seventeen responses that fell outside the contract
were this server, and the other four were argument-type dumps (see
`arg_errors`).

`ok` is NOT removed. Renaming it would fix the contract and break every existing
caller and every one of this repo's own tests, which is the trade `arg_alias`
and `value_alias` already refused twice for parameter names and values. The
response carries both, `success` mirroring `ok`, and `ok` stays the key this
server's own code writes.

Wrapping `tool.fn` rather than `call_tool` follows `json_safe.sanitize_responses`:
the tool body's own dict is the thing that needs the extra key, and doing it
there keeps the two FastMCP transport flavours out of the question entirely.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any


def with_success(payload: Any) -> Any:
    """Mirror `ok` into `success`, leaving everything else exactly as it was."""
    if not isinstance(payload, dict) or "ok" not in payload or "success" in payload:
        return payload
    # Rebuilt rather than mutated so `success` lands next to `ok` instead of at
    # the end: a caller reading the first lines of a truncated response should
    # see the verdict, and dict order is preserved in the JSON that goes out.
    out: dict[str, Any] = {}
    for key, value in payload.items():
        out[key] = value
        if key == "ok":
            out["success"] = value
    return out


def mirror_success(mcp: Any) -> None:
    """Give every registered tool's response a `success` beside its `ok`."""
    for tool in mcp._tool_manager._tools.values():
        fn = getattr(tool, "fn", None)
        if fn is None or getattr(fn, "__envelope_wrapped__", False):
            continue
        tool.fn = _mirrored(fn)


def _mirrored(fn: Any) -> Any:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def mirrored_async(*a: Any, **kw: Any) -> Any:
            return with_success(await fn(*a, **kw))

        mirrored_async.__envelope_wrapped__ = True  # type: ignore[attr-defined]
        return mirrored_async

    @functools.wraps(fn)
    def mirrored(*a: Any, **kw: Any) -> Any:
        return with_success(fn(*a, **kw))

    mirrored.__envelope_wrapped__ = True  # type: ignore[attr-defined]
    return mirrored
