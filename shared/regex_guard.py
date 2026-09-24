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

The worker is a plain `python -c` child that runs this one file, which
imports nothing but the standard library, and it talks over its own stdin and
stdout. Two alternatives were measured first and rejected:

- the third-party `regex` module's in-process timeout fired 1.5-3x late (a 2s
  limit raised at 6.1s), and once let a 3.2s match finish under a 2s limit;
- multiprocessing, as MCP_Documents' core/scan.py uses it: forkserver and
  spawn both import the server's own __main__ into the worker. In the DA
  container that left a 256 MB forkserver resident and took 18s to start
  under load, to run one pattern.

The same file ships in DA, FS, Documents and Web_Browser.
"""

from __future__ import annotations

import os
import pickle
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from typing import IO, Any

# Texts per message: one round trip per batch rather than per cell or line.
BATCH = 5000
# How long a worker may take to start. Not charged to the pattern.
START_SECONDS = 60.0
_WORKER = "__regex_worker__"


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


def _send(stream: IO[bytes], obj: Any) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(struct.pack(">Q", len(data)) + data)
    stream.flush()


def _recv(stream: IO[bytes]) -> Any:
    head = stream.read(8)
    if len(head) < 8:
        raise EOFError
    (size,) = struct.unpack(">Q", head)
    data = stream.read(size)
    if len(data) < size:
        raise EOFError
    return pickle.loads(data)


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


def _serve() -> None:
    """The worker: compile once, say so, then answer until the input closes."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    pattern, flags = _recv(stdin)
    compiled = re.compile(pattern, flags)
    _send(stdout, ("ready", None))
    while True:
        try:
            message = _recv(stdin)
        except EOFError:
            return
        try:
            _send(stdout, ("ok", _answer(compiled, *message)))
        except re.error as exc:  # e.g. a group the replacement names but the pattern lacks
            _send(stdout, ("re.error", str(exc)))


def _read(stream: IO[bytes], answers: queue.Queue[tuple[str, Any]]) -> None:
    """The server's side: every answer the worker sends, then one 'eof'."""
    while True:
        try:
            answers.put(_recv(stream))
        except EOFError, OSError, ValueError, pickle.UnpicklingError:
            answers.put(("eof", None))
            return


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
        self._process: subprocess.Popen[bytes] | None = None
        self._answers: queue.Queue[tuple[str, Any]] = queue.Queue()

    def __enter__(self) -> Guard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _start(self) -> subprocess.Popen[bytes]:
        code = f"import runpy; runpy.run_path({os.path.abspath(__file__)!r}, run_name={_WORKER!r})"
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._process = process
        self._answers = queue.Queue()  # a dead worker's 'eof' must not answer the next one
        assert process.stdin is not None and process.stdout is not None
        threading.Thread(target=_read, args=(process.stdout, self._answers), daemon=True).start()
        _send(process.stdin, (self.pattern, self.flags))
        try:
            kind, _ = self._answers.get(timeout=START_SECONDS)
        except queue.Empty:
            kind = "eof"
        if kind != "ready":
            self.close(stuck=True)
            raise RuntimeError(f"the worker for pattern {self.pattern!r} did not start")
        return process

    def _ask(self, op: str, payload: Any) -> Any:
        process = self._process if self._process is not None else self._start()
        assert process.stdin is not None
        left = self.limit - self.spent
        if left <= 0:
            self.close(stuck=True)
            raise PatternTimeout(self.pattern, self.limit)
        began = time.monotonic()
        try:
            _send(process.stdin, (op, payload))
            kind, answer = self._answers.get(timeout=left)
        except queue.Empty:
            self.close(stuck=True)
            raise PatternTimeout(self.pattern, self.limit) from None
        except OSError:
            kind, answer = "eof", None
        finally:
            self.spent += time.monotonic() - began
        if kind == "eof":
            self.close(stuck=True)
            raise RuntimeError(f"the worker matching {self.pattern!r} stopped without answering")
        if kind == "re.error":
            raise re.error(str(answer))
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
        """Stop the worker: its input closed so it ends, or -- `stuck` in a match -- killed at once."""
        process, self._process = self._process, None
        if process is None:
            return
        if not stuck:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=1)
            except OSError, subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


if __name__ == _WORKER:
    _serve()
