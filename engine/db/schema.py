"""SQLite schema for the engine. Single function: `init_schema(conn)`.

Runs the same DDL as `db/schema.ts` in krawl, using stdlib `sqlite3`. WAL
mode + 64 MB page cache + foreign keys are enabled. All `CREATE TABLE`
and `CREATE INDEX` statements are idempotent (`IF NOT EXISTS`).
"""

from __future__ import annotations

import sqlite3

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous  = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA cache_size   = -64000",  # 64 MB
    "PRAGMA busy_timeout = 5000",
)

_DDL_CORE = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT UNIQUE NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    total_tasks  INTEGER DEFAULT 0,
    completed    INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    instance_id  TEXT,
    config       TEXT
);

CREATE TABLE IF NOT EXISTS task_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    task_name    TEXT,
    url          TEXT NOT NULL,
    mode         TEXT,
    status       TEXT,
    elapsed_ms   INTEGER,
    error        TEXT,
    ts           TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_task_log_run ON task_log(run_id);
CREATE INDEX IF NOT EXISTS idx_task_log_url ON task_log(url);

CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    domain       TEXT NOT NULL,
    title        TEXT,
    status       TEXT,
    mode         TEXT,
    content_hash TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    elapsed_ms   INTEGER,
    source_group TEXT,
    run_id       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_url       ON pages(url);
CREATE INDEX        IF NOT EXISTS idx_pages_domain    ON pages(domain);
CREATE INDEX        IF NOT EXISTS idx_pages_last_seen ON pages(last_seen);
"""

_DDL_CONTENT = """
CREATE TABLE IF NOT EXISTS stocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    company_name TEXT,
    price        REAL,
    change_val   REAL,
    change_pct   REAL,
    volume       REAL,
    market_cap   REAL,
    day_high     REAL,
    day_low      REAL,
    prev_close   REAL,
    week52_high  REAL,
    week52_low   REAL,
    currency     TEXT DEFAULT 'IDR',
    exchange     TEXT,
    source_url   TEXT,
    extracted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stocks_ticker ON stocks(ticker);
CREATE INDEX IF NOT EXISTS idx_stocks_date   ON stocks(extracted_at);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    headline     TEXT NOT NULL,
    url          TEXT,
    content      TEXT,
    published_at TEXT,
    extracted_at TEXT NOT NULL,
    content_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_hash
    ON news(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);
CREATE INDEX IF NOT EXISTS idx_news_date   ON news(extracted_at);

CREATE TABLE IF NOT EXISTS market_indices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name   TEXT NOT NULL,
    price        REAL,
    change_val   TEXT,
    change_pct   TEXT,
    source_url   TEXT,
    extracted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mindices_name ON market_indices(index_name);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    discovered_from TEXT,
    filename        TEXT,
    ext             TEXT,
    size_bytes      INTEGER,
    content_hash    TEXT,
    local_path      TEXT,
    content_text    TEXT,
    extracted_at    TEXT NOT NULL,
    status          TEXT DEFAULT 'discovered'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_hash
    ON files(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);
"""

_DDL_GRAPH = """
CREATE TABLE IF NOT EXISTS links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_url      TEXT NOT NULL,
    to_url        TEXT NOT NULL,
    anchor_text   TEXT,
    discovered_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_pair ON links(from_url, to_url);

CREATE TABLE IF NOT EXISTS domains (
    domain        TEXT PRIMARY KEY,
    mode          TEXT,
    last_seen     TEXT,
    total_pages   INTEGER DEFAULT 0,
    total_errors  INTEGER DEFAULT 0,
    avg_ms        REAL,
    circuit_state TEXT DEFAULT 'closed',
    notes         TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    method          TEXT DEFAULT 'GET',
    discovered_from TEXT,
    params          TEXT,
    response_schema TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_endpoints_url ON endpoints(url);

CREATE TABLE IF NOT EXISTS selector_store (
    domain      TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    selector    TEXT    NOT NULL,
    tag         TEXT    NOT NULL DEFAULT '',
    text_sample TEXT    NOT NULL DEFAULT '',
    classes     TEXT    NOT NULL DEFAULT '',
    el_id       TEXT    NOT NULL DEFAULT '',
    depth       INTEGER NOT NULL DEFAULT 0,
    parent_tag  TEXT    NOT NULL DEFAULT '',
    attrs       TEXT    NOT NULL DEFAULT '[]',
    success_at  TEXT    NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (domain, key)
);
"""

_DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_pages USING fts5(
    url, title, content, source,
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_news USING fts5(
    headline, source, url, content,
    tokenize = 'porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(
    filename, source_url, ext, content_text,
    tokenize = 'unicode61'
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply pragmas and create all tables / FTS5 virtual tables.

    Idempotent: safe to call on every engine startup.
    """
    cur = conn.cursor()
    for pragma in _PRAGMAS:
        cur.execute(pragma)
    cur.executescript(_DDL_CORE)
    cur.executescript(_DDL_CONTENT)
    cur.executescript(_DDL_GRAPH)
    cur.executescript(_DDL_FTS)
    conn.commit()
