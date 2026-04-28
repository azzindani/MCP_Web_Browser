"""Crawl tier — imported and re-exported by engine/__init__.py build step."""
# This is a staging file used during the incremental build of engine/__init__.py.
# Its content will be merged in the next commit.

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from engine.config.defaults import DEFAULTS
from engine.core.checkpoint import Checkpoint
from engine.workers.crawl_worker import CrawlTask, CrawlWorker
from shared.handover import next_step
from shared.platform_utils import get_max_depth, get_max_pages
from shared.progress import fail, info, ok, warn
from shared.receipt import append_receipt


def _crawl_worker_factory(rt: Any) -> CrawlWorker:
    return CrawlWorker(rt.http_client(), rt._breaker, rt._limiter)


async def _crawl_locate(probe_one: Any, url: str) -> dict[str, Any]:
    base = await probe_one(url)
    base["op"] = "crawl_locate"
    base["base_path"] = urlparse(url).path or "/"
    base["suggested_next"] = [
        next_step("crawl_plan", "enumerate would-be frontier (dry-run)"),
        next_step("crawl_run", "start bounded crawl"),
    ]
    return base
