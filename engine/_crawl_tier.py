# ── Crawl tier ───────────────────────────────────────────────────────────────


def _crawl_worker() -> CrawlWorker:
    rt = runtime()
    return CrawlWorker(rt.http_client(), rt._breaker, rt._limiter)


async def crawl_locate(url: str) -> dict[str, Any]:
    base = await probe_one(url)
    base["op"] = "crawl_locate"
    base["base_path"] = urlparse(url).path or "/"
    base["suggested_next"] = [
        next_step("crawl_plan", "enumerate would-be frontier (dry-run)"),
        next_step("crawl_run", "start bounded crawl"),
    ]
    return base


async def crawl_plan(url: str, max_links: int = 25) -> dict[str, Any]:
    worker = _crawl_worker()
    report = await worker.run(CrawlTask(url=url, crawl_depth=1, max_pages=1))
    if not report.pages:
        res: dict[str, Any] = {
            "ok": False, "op": "crawl_plan", "error": "fetch_failed",
            "progress": [fail("Seed fetch failed", url)],
            "hint": "Use crawl_locate() to probe mode, or browse_inspect() for bot-walls.",
            "suggested_next": [
                next_step("crawl_locate", "probe crawl mode for this URL"),
                next_step("browse_inspect", "check for bot-walls"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    seed = report.pages[0]
    res = {
        "ok": seed.status == "ok", "op": "crawl_plan",
        "url": seed.url, "title": seed.title,
        "links": seed.links[:max_links], "files": seed.files[:max_links],
        "elapsed_ms": seed.elapsed_ms,
        "progress": [ok("Frontier enumerated", f"{len(seed.links)} links, {len(seed.files)} files")],
        "suggested_next": [next_step("crawl_run", f"crawl up to {get_max_pages()} pages")],
        "carry_forward": {"url": url},
    }
    res["token_estimate"] = _tok(res)
    return res


async def crawl_run(
    url: str,
    max_pages: int | None = None,
    max_depth: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    rt = runtime()
    pages_cap = max_pages if max_pages is not None else get_max_pages()
    depth_cap = max_depth if max_depth is not None else get_max_depth()
    rid = run_id or f"crawl-{int(time.time())}"
    cp = Checkpoint(f"krawl_checkpoint_{rid}.json", run_id=rid)
    worker = _crawl_worker()
    report = await worker.run(
        CrawlTask(url=url, crawl_depth=depth_cap, max_pages=pages_cap),
        checkpoint=cp,
    )
    indexer = rt.indexer()
    for page in report.pages:
        if page.status != "ok":
            continue
        indexer.index(
            {
                "url": page.url, "title": page.title,
                "status": page.status, "mode": page.mode,
                "elapsed_ms": page.elapsed_ms,
                "extracted": page.extracted, "group": page.group,
                "links": page.links, "extractedAt": page.extracted_at,
            },
            run_id=rid,
        )
    ok_pages = len(report.pages) - report.errors
    progress = [ok("Crawl complete", f"{ok_pages}/{len(report.pages)} pages indexed")]
    if report.errors:
        progress.append(warn("Errors", f"{report.errors} pages failed"))
    if report.files_discovered:
        progress.append(info("Files found", str(report.files_discovered)))
    append_receipt(
        DEFAULTS.DB_PATH, op="crawl_run",
        args={"url": url, "max_pages": pages_cap, "max_depth": depth_cap, "run_id": rid},
        result=f"{ok_pages} pages indexed, {report.errors} errors",
    )
    res: dict[str, Any] = {
        "ok": report.errors < len(report.pages), "op": "crawl_run",
        "run_id": report.run_id, "seed_url": report.seed_url,
        "pages": len(report.pages), "errors": report.errors,
        "files_discovered": report.files_discovered,
        "started_at": report.started_at, "finished_at": report.finished_at,
        "progress": progress,
        "suggested_next": [
            next_step("crawl_verify", f"verify run {rid}"),
            next_step("query_search", "search crawled content"),
            next_step("browse_extract", "extract structured data from a crawled URL"),
        ],
        "carry_forward": {"run_id": rid},
    }
    res["token_estimate"] = _tok(res)
    return res


async def crawl_resume(run_id: str, url: str, max_pages: int | None = None) -> dict[str, Any]:
    return await crawl_run(url, max_pages=max_pages, run_id=run_id)


def crawl_verify(run_id: str) -> dict[str, Any]:
    rt = runtime()
    rows = rt.query().select(
        "SELECT COUNT(*) AS pages, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
        "FROM task_log WHERE run_id = ?",
        params=(run_id,), limit=1,
    )
    if not rows:
        res: dict[str, Any] = {
            "ok": False, "op": "crawl_verify", "run_id": run_id, "error": "not_found",
            "progress": [fail("Run not found", run_id)],
            "hint": "Run ID not in task_log. Call crawl_run() first or query_locate() to list data.",
            "suggested_next": [next_step("query_locate", "list tables and check task_log")],
        }
        res["token_estimate"] = _tok(res)
        return res
    row = rows[0]
    pages = int(row["pages"] or 0)
    errors = int(row["errors"] or 0)
    progress = [ok("Run verified", f"{pages} pages, {errors} errors")]
    if errors:
        progress.append(warn("Errors present", f"{errors} failed pages"))
    res = {
        "ok": True, "op": "crawl_verify", "run_id": run_id,
        "pages": pages, "errors": errors, "progress": progress,
        "suggested_next": [
            next_step("query_search", "search crawled content"),
            next_step("browse_extract", "extract structured data from a crawled URL"),
            next_step("crawl_resume", "resume crawl if incomplete"),
        ],
    }
    res["token_estimate"] = _tok(res)
    return res


# ── Extract tier ─────────────────────────────────────────────────────────────


async def extract_from_url(
    url: str,
    selector: str,
    mode: str = "css",
    output_type: str = "text",
    limit: int | None = None,
) -> dict[str, Any]:
    from engine.workers.extractor import HtmlExtractor
    rt = runtime()
    cap = limit if limit is not None else get_max_results()
    result = await rt.http_worker().fetch_one(Task(url=url))
    if result.status != "ok":
        res: dict[str, Any] = {
            "ok": False, "op": "browse_extract", "url": url,
            "error": f"fetch failed: {result.error or result.status}",
            "progress": [fail("Fetch failed", result.error or result.status)],
            "hint": "Use browse_locate() to probe mode or browse_inspect() for bot-walls.",
            "suggested_next": [
                next_step("browse_locate", "probe URL mode"),
                next_step("browse_inspect", "check for bot-walls"),
            ],
        }
        res["token_estimate"] = _tok(res)
        return res
    if not result.raw_html:
        res = {
            "ok": False, "op": "browse_extract", "url": url,
            "error": "response is not HTML (JSON or binary content)",
            "progress": [fail("Not HTML", "cannot apply CSS/XPath to non-HTML response")],
            "hint": "This URL returns JSON/binary. Use browse_fetch() + query_select() instead.",
            "suggested_next": [next_step("browse_fetch", "fetch and index JSON data")],
        }
        res["token_estimate"] = _tok(res)
        return res
    try:
        ex = HtmlExtractor(result.raw_html, base_url=url)
    except RuntimeError as exc:
        res = {
            "ok": False, "op": "browse_extract", "url": url, "error": str(exc),
            "progress": [fail("Extractor unavailable", str(exc)[:80])],
            "hint": "Install lxml: pip install 'lxml>=4.9' 'cssselect>=1.2'",
        }
        res["token_estimate"] = _tok(res)
        return res
    if mode == "css":
        extraction = ex.css(selector, output_type=output_type, limit=cap)
    elif mode == "xpath":
        extraction = ex.xpath(selector, output_type=output_type, limit=cap)
    elif mode == "text":
        extraction = ex.find_by_text(selector, output_type=output_type, limit=cap)
    elif mode == "regex":
        extraction = ex.find_by_regex(selector, output_type=output_type, limit=cap)
    else:
        res = {
            "ok": False, "op": "browse_extract", "url": url,
            "error": f"unknown mode: {mode!r}",
            "hint": "mode must be css|xpath|text|regex",
            "progress": [fail("Unknown mode", mode)],
        }
        res["token_estimate"] = _tok(res)
        return res
    if not extraction.ok:
        res = {
            "ok": False, "op": "browse_extract", "url": url,
            "selector": selector, "mode": mode, "error": extraction.error,
            "progress": [fail("Extraction failed", extraction.error or "")],
            "hint": f"Check {mode} selector syntax. Use browse_inspect() to preview the page.",
            "suggested_next": [next_step("browse_inspect", "preview page HTML structure")],
        }
        res["token_estimate"] = _tok(res)
        return res
    progress = [ok("Extracted", f"{extraction.count} matches via {mode}:{selector}")]
    if extraction.truncated:
        progress.append(warn("Truncated", f"showing {cap} of {extraction.count} matches"))
    res = {
        "ok": True, "op": "browse_extract",
        "url": url, "selector": selector, "mode": mode, "output_type": output_type,
        "matches": extraction.matches, "count": extraction.count,
        "truncated": extraction.truncated, "elapsed_ms": result.elapsed_ms,
        "progress": progress,
        "suggested_next": [
            next_step("browse_extract", "run another selector on this page"),
            next_step("query_search", "search other indexed pages"),
        ],
        "carry_forward": {"url": url},
    }
    res["token_estimate"] = _tok(res)
    return res
