"""Refuse an argument a tool does not take, instead of ignoring it.

This repo used to refuse. `shared/arg_errors.py`, written when it did, says so
in its own docstring -- *"`enforce_known_arguments` looks like it covers this
and does not: it catches an unknown argument NAME, while a known name with the
wrong type never reaches it"* -- and built the type half on the assumption that
the name half was already there. It was, once: the fleet ran standalone
fastmcp 2.x, which forbids extra arguments outright.

The move to the official `mcp` SDK removed it silently. The bundled FastMCP
builds each tool's argument model with pydantic's default `extra="ignore"`, so
an argument no tool declares is dropped without a word and the call succeeds.
Two repos had already written this guard for their own reasons and kept it;
five inherited `extra="ignore"` and lost the check without a line changing.

What that cost, measured live against the deployed servers in round 27:

    aggregate_dataset(..., agg_func="mean")   -> success: true, SUMS returned
    aggregate_dataset(..., banana="yes")      -> success: true
    train_regressor(..., feature_columns=[…]) -> success: true, all features used,
                                                 identical metrics, leak still in
    apply_patch(..., output_path="new.csv")   -> success: true, and the SOURCE
                                                 file was overwritten instead

The last one is why this is not a tidiness fix. `apply_patch` edits in place by
design; `output_path` is not one of its parameters, so an explicit instruction
to write somewhere else was discarded in silence and the caller's input file
lost a column. Only the snapshot `apply_patch` takes first made that recoverable.

Meanwhile the same server rejects an unknown field one level down, inside an
op, with a did-you-mean:

    ops=[{"op": "drop_column", "column": "x"}]
      -> Op 0 (drop_column): unknown field(s) column -- did you mean columns?

The check was already written. It was applied to the nested op grammar and not
to the tool signature. This module applies it to the signature.

`enforce_known_arguments(mcp)` checks argument names against the tool's own
schema before dispatch. The refusal *lists the names the tool accepts*, so a
caller can fix the call from the response rather than guessing again.

Install it last, so it wraps the others and an unknown name is answered as an
unknown name rather than reaching any later guard.

Applied once per server at start; no tool body changes.
"""

from __future__ import annotations

from typing import Any


def _did_you_mean(unknown: str, known: list[str]) -> str:
    """The closest accepted name, when one is obviously close."""
    import difflib

    # Underscore-insensitive first: type/type_ and format/format_ are the real
    # cases here and difflib alone rates them no higher than unrelated names.
    stripped = {k.rstrip("_"): k for k in known}
    if unknown.rstrip("_") in stripped:
        return stripped[unknown.rstrip("_")]
    close = difflib.get_close_matches(unknown, known, n=1, cutoff=0.75)
    return close[0] if close else ""


def enforce_known_arguments(mcp: Any) -> None:
    """Make every tool on this server refuse an argument it does not declare."""
    manager = mcp._tool_manager
    original = manager.call_tool

    async def call_tool(
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        tool = manager.get_tool(name)
        if tool is not None and isinstance(arguments, dict):
            schema = tool.parameters or {}
            known = sorted(schema.get("properties", {}))
            unknown = [k for k in arguments if k not in known]
            # Gate on the schema being readable, not on it being non-empty. The
            # first version tested `unknown and known`, which meant a tool
            # taking NO arguments -- browse_datetime, browse_status -- accepted
            # every argument in silence, because its `known` list is empty and
            # the guard fell straight through. An explicit `properties: {}` is
            # a tool that takes nothing, which is a fact worth enforcing; a
            # missing `properties` is a schema we cannot read, and there the
            # old caution is right.
            if unknown and "properties" in schema:
                first = unknown[0]
                suggestion = _did_you_mean(first, known)
                # The accepted names go in once. The first version of this
                # appended "Accepted: <names>." to a hint that had already
                # spelled the same list out, shipping every parameter name
                # twice on servers whose whole point is a tight context.
                lead = f"Did you mean {suggestion}=? " if suggestion else ""
                refusal = {
                    "success": False,
                    "op": name,
                    "error": f"{name} does not take {', '.join(unknown)}",
                    "hint": f"{lead}{name} accepts: {', '.join(known)}.",
                    "progress": [],
                }
                # Measured, not assumed: a flat estimate was under half the real
                # size for a wide tool, and a client that budgets from it admits
                # a response twice the size it was told to expect.
                refusal["token_estimate"] = len(str(refusal)) // 4
                # The server asks for the converted form. Returning the raw dict
                # -- or worse a JSON string, which the SDK then iterates one
                # character at a time into 1900 validation errors -- produces a
                # response no client can read. Convert it exactly as this tool's
                # own return value would have been.
                if convert_result:
                    return tool.fn_metadata.convert_result(refusal)
                return refusal
        return await original(name, arguments, context, convert_result)

    manager.call_tool = call_tool
