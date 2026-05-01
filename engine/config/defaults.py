"""Engine-wide defaults. One source of truth for every tunable.

Mirrors `config/defaults.ts` in krawl. Per-call hardware caps live in
`shared.platform_utils`; constants here are operational defaults that
do not change between constrained and full mode.
"""

from __future__ import annotations

from typing import Final


class DEFAULTS:
    # Worker pool sizes (engine concurrency, not LLM context caps).
    HTTP_CONCURRENCY: Final[int] = 5
    BROWSER_CONTEXTS: Final[int] = 3
    CRAWL_CONCURRENCY: Final[int] = 2

    # Timeouts (seconds).
    HTTP_TIMEOUT: Final[float] = 15.0
    BROWSER_TIMEOUT: Final[float] = 30.0
    CRAWL_TIMEOUT: Final[float] = 20.0
    WAIT_FOR_DATA_TIMEOUT: Final[float] = 25.0

    # Retry / backoff (ms — kept in ms to match krawl semantics).
    MAX_RETRIES: Final[int] = 3
    BACKOFF_BASE_MS: Final[int] = 2_000
    BACKOFF_MAX_MS: Final[int] = 30_000

    # Circuit breaker.
    CIRCUIT_THRESHOLD: Final[int] = 5
    CIRCUIT_RESET_MS: Final[int] = 300_000  # 5 min half-open delay

    # Rate limiting (requests per second per domain).
    DEFAULT_RPS: Final[float] = 0.5
    KNOWN_SAFE_RPS: Final[float] = 2.0

    # Crawl bounds. Honour platform_utils when used in tool handlers.
    MAX_CRAWL_DEPTH: Final[int] = 3
    MAX_CRAWL_PAGES: Final[int] = 50

    # Checkpoint cadence.
    CHECKPOINT_EVERY: Final[int] = 10

    # Storage paths (relative to MCP_DATA_ROOT).
    DB_PATH: Final[str] = "krawl.db"
    JSONL_PATH: Final[str] = "krawl_output.jsonl"
    FILES_DIR: Final[str] = "krawl_files"

    # Browser environment.
    VIEWPORT: Final[dict[str, int]] = {"width": 1280, "height": 800}
    USER_AGENT: Final[str] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    LOCALE: Final[str] = "id-ID"
    TIMEZONE: Final[str] = "Asia/Jakarta"

    CHROMIUM_ARGS: Final[tuple[str, ...]] = (
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-zygote",
        "--disable-extensions",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-features=TranslateUI",
    )

    COLLECTIBLE_EXTENSIONS: Final[tuple[str, ...]] = (
        ".pdf",
        ".xls",
        ".xlsx",
        ".csv",
        ".zip",
        ".docx",
        ".ppt",
        ".pptx",
    )

    CHALLENGE_TITLES: Final[tuple[str, ...]] = (
        "just a moment",
        "tunggu sebentar",
        "attention required",
        "verifikasi keamanan",
        "ddos-guard",
    )

    SPA_MARKERS: Final[tuple[str, ...]] = (
        "ng-version",
        "data-reactroot",
        "__NEXT_DATA__",
        "nuxt",
        "__vue",
    )


BOT_BODY_PATTERNS: Final[tuple[str, ...]] = (
    "please enable cookies",
    "enable javascript",
    "checking your browser",
    "browser check",
    "ddos-guard",
    "ray id",
    "security check",
    "access denied",
    "your ip has been blocked",
    "bot protection",
    "automated access",
    "suspicious activity",
)
