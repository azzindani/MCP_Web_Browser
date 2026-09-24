"""browse_extract(mode='regex') ran a caller's pattern with nothing bounding it.

The extractor matched every element's text with a bare `re` pattern, and `re`
has no timeout: a pattern with nested repeats, over a paragraph of forty a's
and a `!`, holds the call for longer than anyone waits, and the page is the
web's, so its text is nobody's choice. It now matches in a worker the server
can stop (shared/regex_guard.py, the file DA, FS and Documents ship too),
under MCP_REGEX_SECONDS:

- a runaway pattern is refused inside the budget, with a hint that says what
  to do instead of blaming the selector's syntax;
- every other pattern picks the same elements, in the same order, and stops
  at the same match, as the `re` loop it replaces.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import engine
from engine.workers.extractor import _SKIP_TAGS, HtmlExtractor

RUNAWAY = r"(a+)+$"
PAGE = (
    "<html><head><title>T</title><script>var x = 'ref 1-a';</script></head><body>"
    "<h1>Orders</h1><p>order 12-A shipped</p><p>none here</p>"
    "<ul>" + "".join(f"<li>ref {i}-b{i}</li>" for i in range(30)) + "</ul>"
    "<p>" + "a" * 40 + "!</p></body></html>"
)


@pytest.fixture(autouse=True)
def _budget(monkeypatch):
    monkeypatch.setenv("MCP_REGEX_SECONDS", "1")


def _reference(html: str, pattern: str, limit: int) -> list[tuple[str, str]]:
    """The loop this replaced, verbatim in effect: page order, stop at limit * 4."""
    compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    ex = HtmlExtractor(html)
    out = []
    for el in ex._doc.iter():
        if str(el.tag) in _SKIP_TAGS:
            continue
        if compiled.search(" ".join(el.itertext()).strip()):
            out.append(el)
            if len(out) >= limit * 4:
                break
    return [(str(el.tag), " ".join(el.itertext()).strip()) for el in out]


class TestARunawayPatternIsStopped:
    def test_the_extractor_refuses_inside_the_budget(self):
        began = time.monotonic()
        result = HtmlExtractor(PAGE).find_by_regex(RUNAWAY)
        assert time.monotonic() - began < 8
        assert result.ok is False and RUNAWAY in (result.error or "")
        assert result.hint and "mode='text'" in result.hint

    def test_the_tool_passes_the_hint_on(self, monkeypatch):
        async def fetch_one(_task):
            return SimpleNamespace(status="ok", raw_html=PAGE, error=None)

        worker = SimpleNamespace(fetch_one=fetch_one)
        monkeypatch.setattr(engine, "runtime", lambda: SimpleNamespace(http_worker=lambda: worker))
        res = asyncio.run(engine.extract_from_url("https://example.test/", RUNAWAY, mode="regex"))
        assert res["ok"] is False and RUNAWAY in res["error"]
        assert "mode='text'" in res["hint"] and "syntax" not in res["hint"]


class TestEveryOtherPatternPicksTheSameElements:
    @pytest.mark.parametrize("pattern", [r"\d+-\w+", r"^none", r"ORDER", r"zzz", r"ref 2\d-"])
    @pytest.mark.parametrize("limit", [1, 3, 20])
    def test_in_page_order_to_the_same_stop(self, pattern, limit):
        result = HtmlExtractor(PAGE).find_by_regex(pattern, output_type="text", limit=limit)
        assert result.ok is True, result.error
        want = _reference(PAGE, pattern, limit)
        assert result.count == len(want) and result.truncated == (len(want) > limit)
        assert [(m["tag"], m["text"]) for m in result.matches] == want[:limit]

    def test_a_bad_pattern_is_the_same_refusal(self):
        result = HtmlExtractor(PAGE).find_by_regex("foo(")
        assert result.ok is False and result.error == "missing ), unterminated subpattern at position 3"


def test_the_guard_is_one_file_across_the_fleet():
    root = Path(__file__).resolve().parents[2]
    mine = hashlib.sha256((root / "shared" / "regex_guard.py").read_bytes()).hexdigest()
    siblings = [
        root.parent / repo / "shared" / "regex_guard.py"
        for repo in ("MCP_Data_Analyst", "MCP_File_System", "MCP_Documents")
    ]
    present = [p for p in siblings if p.exists()]
    if not present:
        pytest.skip("no sibling repo checked out beside this one")
    for p in present:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == mine, p
