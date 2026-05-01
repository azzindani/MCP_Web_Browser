"""Verify all @mcp.tool() docstrings are <= 80 characters.

CI gate: exits non-zero if any tool docstring exceeds the limit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MAX_LEN = 80

SERVER_FILES = [
    Path("server.py"),
]


def check_file(path: Path) -> list[str]:
    """Return list of error strings for docstrings exceeding MAX_LEN."""
    errors: list[str] = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        is_tool = False
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if dec.func.attr == "tool":
                    is_tool = True
                    break
        if not is_tool:
            continue

        docstring = ast.get_docstring(node) or ""
        first_line = docstring.splitlines()[0] if docstring else ""
        if len(first_line) > MAX_LEN:
            errors.append(
                f"  {path}:{node.lineno} — {node.name}() "
                f"first line is {len(first_line)} chars (max {MAX_LEN}): "
                f"{first_line!r}"
            )

    return errors


def main() -> int:
    all_errors: list[str] = []
    for server_file in SERVER_FILES:
        if not server_file.exists():
            print(f"WARNING: {server_file} not found, skipping")
            continue
        all_errors.extend(check_file(server_file))

    if all_errors:
        print(f"FAIL — {len(all_errors)} docstring(s) exceed {MAX_LEN} chars:")
        for err in all_errors:
            print(err)
        return 1

    print(f"OK — all tool docstrings <= {MAX_LEN} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
