"""Priority task queue. Lower priority number = processed first.

Mirrors core/queue.ts in krawl. Tasks with equal priority are FIFO
(insertion-order tie-breaking via a monotone sequence counter).
heapq gives O(log n) enqueue / dequeue.
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return f"task_{int(time.monotonic() * 1e6)}"


@dataclass
class Task:
    id: str
    name: str
    url: str
    mode: str           # auto | http_json | http_curl | browser | crawl | blocked
    priority: int       # 1 = highest
    group: str
    tags: list[str]
    retries: int
    max_retries: int
    status: str         # pending | running | done | failed | skipped | dead_letter
    crawl_depth: int | None = None
    collect_files: list[str] | None = None
    extract_type: str | None = None
    selectors: dict[str, str] | None = None
    parent_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


def make_task(
    url: str,
    name: str = "",
    mode: str = "auto",
    priority: int = 5,
    group: str = "default",
    tags: list[str] | None = None,
    max_retries: int = 3,
    crawl_depth: int | None = None,
    collect_files: list[str] | None = None,
    extract_type: str | None = None,
    selectors: dict[str, str] | None = None,
    parent_id: str | None = None,
) -> Task:
    try:
        hostname = urlparse(url).hostname or url
    except ValueError:
        hostname = url
    return Task(
        id=_new_id(),
        name=name or hostname,
        url=url,
        mode=mode,
        priority=priority,
        group=group,
        tags=tags or [],
        retries=0,
        max_retries=max_retries,
        status="pending",
        crawl_depth=crawl_depth,
        collect_files=collect_files,
        extract_type=extract_type,
        selectors=selectors,
        parent_id=parent_id,
    )


class TaskQueue:
    """Priority queue backed by heapq.

    Heap entries are (priority, sequence, Task) triples so equal
    priorities preserve FIFO order without comparing Task objects.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Task]] = []
        self._seq: int = 0
        self._running: dict[str, Task] = {}   # id → task
        self._done: dict[str, Task] = {}      # url → task
        self._dead: list[Task] = []
        self._pending_urls: set[str] = set()

    # ── enqueue / dequeue ──────────────────────────────────────────

    def enqueue(self, task: Task) -> None:
        if task.url in self._done or task.url in self._pending_urls:
            return
        self._seq += 1
        heapq.heappush(self._heap, (task.priority, self._seq, task))
        self._pending_urls.add(task.url)

    def enqueue_many(self, tasks: list[Task]) -> None:
        for t in tasks:
            self.enqueue(t)

    def dequeue(self) -> Task | None:
        while self._heap:
            _, _, task = heapq.heappop(self._heap)
            self._pending_urls.discard(task.url)
            task.status = "running"
            task.started_at = _now_iso()
            self._running[task.id] = task
            return task
        return None

    def drain_pending(self) -> list[Task]:
        """Remove and return all pending tasks without touching running/done."""
        tasks = [t for _, _, t in self._heap]
        self._heap.clear()
        self._pending_urls.clear()
        return tasks

    # ── lifecycle ──────────────────────────────────────────────────

    def mark_done(self, task_id: str) -> None:
        task = self._running.pop(task_id, None)
        if task is None:
            return
        task.status = "done"
        task.finished_at = _now_iso()
        self._done[task.url] = task

    def mark_failed(self, task_id: str, error: str) -> None:
        task = self._running.pop(task_id, None)
        if task is None:
            return
        task.error = error
        task.retries += 1
        if task.retries > task.max_retries:
            task.status = "dead_letter"
            self._dead.append(task)
        else:
            task.status = "pending"
            task.priority += task.retries  # demote: higher number = lower priority
            self.enqueue(task)

    def spawn_from_crawl(self, parent: Task, urls: list[str]) -> list[Task]:
        """Create child tasks from discovered links, one depth level lower."""
        depth = (parent.crawl_depth or 0) - 1
        if depth < 0:
            return []
        known = set(self._done.keys()) | self._pending_urls
        children: list[Task] = []
        for url in urls:
            if url in known:
                continue
            child = make_task(
                url=url,
                mode="auto",
                priority=parent.priority + 1,
                group=parent.group,
                tags=list(parent.tags),
                crawl_depth=depth,
                parent_id=parent.id,
            )
            children.append(child)
            known.add(url)
        self.enqueue_many(children)
        return children

    # ── predicates ────────────────────────────────────────────────

    def is_done_url(self, url: str) -> bool:
        return url in self._done

    def get_done_urls(self) -> set[str]:
        return set(self._done.keys())

    def get_dead_letter(self) -> list[Task]:
        return list(self._dead)

    @property
    def pending_count(self) -> int:
        return len(self._heap)

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def done_count(self) -> int:
        return len(self._done)

    @property
    def dead_count(self) -> int:
        return len(self._dead)

    @property
    def total_count(self) -> int:
        return self.pending_count + self.running_count + self.done_count

    @property
    def is_empty(self) -> bool:
        return self.pending_count == 0 and self.running_count == 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending"  : self.pending_count,
            "running"  : self.running_count,
            "done"     : self.done_count,
            "dead"     : self.dead_count,
            "done_urls": sorted(self._done.keys()),
        }

    def load_snapshot(self, snap: dict[str, Any]) -> None:
        """Pre-populate done set from a checkpoint snapshot."""
        for url in snap.get("done_urls") or []:
            placeholder = make_task(url=url, name=url)
            placeholder.status = "done"
            self._done[url] = placeholder
