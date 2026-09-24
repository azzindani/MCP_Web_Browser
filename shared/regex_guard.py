"""A caller's regular expression, run where it can be stopped.

Python's `re` has no timeout and no step limit. A pattern with nested repeats
-- `(a+)+$`, `(a|aa)+$` -- tries every way to split its text before it can say
no, and on a 29-character cell or file that is longer than anyone will wait.
Measured 2026-09-24: DA's extract_regex, FS's content search (both modes) and
FS's replace_text were each still running when killed at 10 seconds. The
server thread never comes back, and nothing in the response, the log or the
health check says why.

So a caller's pattern runs in a worker process, and the server waits on it
with a budget. The budget counts only the time spent matching -- reading a
thousand files is not the pattern's fault -- and when it runs out the worker
is killed and PatternTimeout names the pattern. A literal (`re.escape`d) query
cannot backtrack and never needs this.

MCP_Documents' `find` has done the same since it measured `(\\s*\\w+)+$` still
running after 120s on one page (core/scan.py). This is that guard made
general: one worker for a whole call, fed in batches, not one per page. The
third-party `regex` module was tried first for its in-process timeout. It
fired 1.5-3x late (a 2s limit raised at 6.1s) and once let a 3.2s match
finish under a 2s limit, so it is not the guard.

The same file ships in DA, FS, Documents and Web_Browser.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import re
import time
from typing import Any

# Texts per message: one round trip per batch rather than per cell or line.
BATCH = 5000
# How long a worker may take to start. Not charged to the pattern: on macOS and
# Windows a spawned worker re-imports the server before it can match anything.
START_SECONDS = 120.0


def seconds() -> float:
    """The matching budget for one call: MCP_REGEX_SECONDS, else 10 (5 constrained)."""
    default = "5" if os.environ.get("MCP_CONSTRAINED_MODE", "0") == "1" else "10"
    try:
        return max(0.01, float(os.environ.get("MCP_REGEX_SECONDS", default)))
    except ValueError:
        return float(default)


class PatternTimeout(ValueError):
    """The pattern was still matching when its budget ran out."""

    def __init__(self, pattern: str, limit: float) -> None:
        super().__init__(
            f"the pattern {pattern!r} was still matching after {limit:g}s and was stopped: "
            "nested repeats such as (a+)+ or (a|aa)+ make a regular expression try every way "
            "to split its text. Rewrite it without them, or match the text literally."
        )
        self.pattern = pattern
        self.limit = limit


def _answer(compiled: re.Pattern[str], op: str, payload: Any) -> Any:
    if op == "found":
        return [compiled.search(t) is not None for t in payload]
    if op == "first":
        texts, group = payload
        out: list[Any] = []
        for t in texts:
            m = compiled.search(t) if isinstance(t, str) else None
            try:
                out.append(None if m is None else m.group(group))
            except IndexError:
                out.append(None)
        return out
    if op == "subn":
        repl, text, count = payload
        return compiled.subn(repl, text, count=count)
    raise ValueError(f"unknown op {op!r}")


def _serve(conn: Any, pattern: str, flags: int) -> None:
    """The worker: compile once, say so, then answer until told to stop."""
    compiled = re.compile(pattern, flags)
    conn.send(("ready", None))
    while True:
        message = conn.recv()
        if message is None:
            break
        try:
            conn.send(("ok", _answer(compiled, *message)))
        except re.error as exc:  # e.g. a group the replacement names but the pattern lacks
            conn.send(("re.error", str(exc)))
    conn.close()


class Guard:
    """One caller pattern, matched in a worker for the whole of one call.

    Use it as a context manager so the worker goes when the call does. A bad
    pattern raises re.error from the constructor, in the caller's process,
    exactly where re.compile did.
    """

    def __init__(self, pattern: str, flags: int = 0, limit: float | None = None) -> None:
        re.compile(pattern, flags)
        self.pattern = pattern
        self.flags = flags
        self.limit = seconds() if limit is None else limit
        self.spent = 0.0
        self._process: Any = None
        self._conn: Any = None

    def __enter__(self) -> Guard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _start(self) -> Any:
        # The platform DEFAULT start method, as in MCP_Documents' core/scan.py:
        # forkserver on Linux since 3.14, spawn on macOS and Windows -- none of
        # them inherits a lock another thread of the server holds. Plain fork
        # does, so it is never used.
        ctx = mp.get_context()
        process_type = ctx.Process
        if ctx.get_start_method() == "fork":
            process_type = mp.get_context("spawn").Process
        mine, theirs = ctx.Pipe()
        process = process_type(target=_serve, args=(theirs, self.pattern, self.flags), daemon=True)
        try:
            process.start()
        except BaseException:
            mine.close()
            raise
        finally:
            theirs.close()  # the worker holds its own end; ours must go or a dead worker is never noticed
        self._process, self._conn = process, mine
        if not mine.poll(START_SECONDS):
            self.close()
            raise RuntimeError(f"the worker for pattern {self.pattern!r} did not start")
        mine.recv()
        return mine

    def _ask(self, op: str, payload: Any) -> Any:
        conn = self._conn if self._conn is not None else self._start()
        left = self.limit - self.spent
        if left <= 0:
            self.close(stuck=True)
            raise PatternTimeout(self.pattern, self.limit)
        began = time.monotonic()
        conn.send((op, payload))
        answered = conn.poll(left)
        self.spent += time.monotonic() - began
        if not answered:
            self.close(stuck=True)
            raise PatternTimeout(self.pattern, self.limit)
        try:
            kind, answer = conn.recv()
        except EOFError:
            self.close(stuck=True)
            message = f"the worker matching {self.pattern!r} stopped without answering"
            raise RuntimeError(message) from None
        if kind == "re.error":
            raise re.error(answer)
        return answer

    def found(self, texts: list[str]) -> list[bool]:
        """Whether the pattern occurs anywhere in each text."""
        out: list[bool] = []
        for i in range(0, len(texts), BATCH):
            out += self._ask("found", texts[i : i + BATCH])
        return out

    def search(self, text: str) -> bool:
        return self.found([text])[0]

    def first(self, texts: list[Any], group: int | str = 0) -> list[Any]:
        """The given group of each text's first match; None for no match, no such group, or a non-text."""
        out: list[Any] = []
        for i in range(0, len(texts), BATCH):
            out += self._ask("first", (texts[i : i + BATCH], group))
        return out

    def subn(self, repl: str, text: str, count: int = 0) -> tuple[str, int]:
        """re.subn, with a template replacement."""
        new, n = self._ask("subn", (repl, text, count))
        return new, n

    def close(self, stuck: bool = False) -> None:
        """Stop the worker: asked to finish, or -- `stuck` in a match -- terminated at once."""
        conn, process = self._conn, self._process
        self._conn = self._process = None
        if conn is not None:
            if not stuck:
                try:
                    conn.send(None)
                except OSError, ValueError:
                    pass
            conn.close()
        if process is not None:
            if not stuck:
                process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
