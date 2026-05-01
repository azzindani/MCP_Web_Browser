"""CI gate: every @app.tool() first-line docstring must be <= 80 characters."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SERVER = Path(__file__).parent.parent / "server.py"
MAX_CHARS = 80


def _is_tool_decorated(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in func.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr == "tool":
            return True
        if isinstance(dec, ast.Call):
            fn = dec.func
            if isinstance(fn, ast.Attribute) and fn.attr == "tool":
                return True
    return False


def check() -> int:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    violations: list[tuple[str, int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_tool_decorated(node):
            continue
        doc = ast.get_docstring(node) or ""
        first_line = doc.strip().splitlines()[0] if doc.strip() else ""
        if len(first_line) > MAX_CHARS:
            violations.append((node.name, len(first_line), first_line))

    if violations:
        for name, n_chars, line in violations:
            print(f"FAIL  {name}: {n_chars} chars  {line!r}", file=sys.stderr)
        print(f"\n{len(violations)} tool(s) exceed {MAX_CHARS}-char docstring cap", file=sys.stderr)
        return 1

    tools = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tool_decorated(node)
    )
    print(f"OK  all {tools} tool docstrings are within {MAX_CHARS} chars")
    return 0


if __name__ == "__main__":
    sys.exit(check())
