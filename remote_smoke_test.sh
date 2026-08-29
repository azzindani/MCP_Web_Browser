#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# mcp_web_browser — remote smoke test.
#
# NOT part of pytest / CI (see CLAUDE.md §7 "Remote smoke tests"). This
# script is the separate, manual/on-demand check that actually exercises the
# deployed HTTP endpoint: real auth enforcement + a real handwritten-prompt-
# style tool call with real data, against the real public domain.
#
# Usage:
#   ./remote_smoke_test.sh                       # reads WEB_API_KEY from .env
#   WEB_API_KEY=sk-... ./remote_smoke_test.sh     # or pass it directly
#   DOMAIN=http://localhost:8766 ./remote_smoke_test.sh   # test a different target
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${DOMAIN:-https://browser.casava.space}"
# Read the key out of .env without executing it. `source` runs every line of
# the file, so a line that is not a KEY=VALUE assignment is a command; that has
# already turned a stray summary line into a file named after a secret. A plain
# read of one assignment cannot do that.
if [ -z "${WEB_API_KEY:-}" ] && [ -f .env ]; then
  WEB_API_KEY=$(sed -n 's/^[[:space:]]*WEB_API_KEY[[:space:]]*=[[:space:]]*//p' .env | tail -n1 | tr -d '\042\047\r')
fi
KEY="${WEB_API_KEY:?Set WEB_API_KEY (env var or .env file) before running}"

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; exit 1; }

echo "Target: $DOMAIN"
echo
echo "== auth enforcement =="

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$DOMAIN/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}')
[ "$code" = "401" ] && pass "no token -> 401" || fail "no token -> expected 401, got $code"

SID=$(curl -s -i -X POST "$DOMAIN/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
  | grep -i mcp-session-id | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "valid token -> session established" || fail "valid token -> no session id returned"

curl -s -X POST "$DOMAIN/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $KEY" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized"}' > /dev/null

call() {
  local id="$1" name="$2" args="$3"
  curl -s -X POST "$DOMAIN/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $KEY" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}"
}

echo
echo '== prompt: "is the browser service up?" -> browse_status =='
RESULT=$(call 3 browse_status '{}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_status succeeded" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "what time is it?" -> browse_datetime =='
RESULT=$(call 4 browse_datetime '{}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_datetime succeeded" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "check if https://example.com is reachable before fetching" -> browse_locate =='
RESULT=$(call 5 browse_locate '{"url":"https://example.com"}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_locate(example.com) probed the real URL" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "inspect https://example.com before I fetch it" -> browse_inspect =='
RESULT=$(call 6 browse_inspect '{"url":"https://example.com"}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_inspect(example.com) inspected the real page" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "fetch https://example.com and index it" -> browse_fetch =='
RESULT=$(call 7 browse_fetch '{"url":"https://example.com"}')
# "ok" is a field of the tool's own document, which arrives escaped inside the
# envelope's text (\"ok\": true); "isError" belongs to the envelope itself and
# does not. Every other assertion in this file already allows for the escaping.
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && echo "$RESULT" | grep -Eq '"isError":[[:space:]]*false' \
  && pass "browse_fetch(example.com) fetched + indexed the real page" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "verify that example.com was indexed correctly" -> browse_verify =='
RESULT=$(call 8 browse_verify '{"url":"https://example.com"}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_verify(example.com) confirmed the real index entry" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "extract the <h1> heading from example.com" -> browse_extract =='
RESULT=$(call 9 browse_extract '{"url":"https://example.com","selector":"h1"}')
echo "$RESULT" | grep -qi 'Example Domain' && pass "browse_extract(example.com, h1) = 'Example Domain'" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "search the web for the Python programming language" -> browse_search =='
RESULT=$(call 10 browse_search '{"query":"Python programming language","limit":3}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "browse_search(Python programming language) returned real results" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "how many pages have been indexed so far?" -> query_locate =='
RESULT=$(call 11 query_locate '{}')
echo "$RESULT" | grep -q '"pages":1' && pass "query_locate reported the real pages table count (1)" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "search the indexed knowledge base for '"'"'Example'"'"'" -> query_search =='
RESULT=$(call 12 query_search '{"query":"Example"}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "query_search(Example) searched the real FTS5 index" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "how many pages are indexed, broken down by status?" -> query_stats =='
RESULT=$(call 13 query_stats '{}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "query_stats computed real counts from the real DB" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "run SQL: which URLs have been indexed?" -> query_select =='
RESULT=$(call 14 query_select '{"sql":"SELECT url, domain, status FROM pages LIMIT 5","limit":5}')
echo "$RESULT" | grep -qi 'example.com' && pass "query_select ran real SQL against the real pages table" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "export the indexed pages table to CSV" -> query_export =='
RESULT=$(call 15 query_export '{"table":"pages","out_path":"/app/exports/pages_export.csv","fmt":"csv"}')
echo "$RESULT" | grep -Eq 'ok\\?":[[:space:]]*true' && pass "query_export wrote a real CSV file on the host" || fail "unexpected result: $RESULT"

echo
echo "===== boundary regression: truncated must be exact at the limit cap, not off-by-one ====="
echo "A prior bug computed 'truncated' from a count already capped during collection,"
echo "which is a false positive exactly when the true count equals the cap. query_select"
echo "is tested with a self-contained WITH RECURSIVE query — no real crawled data needed,"
echo "no side effects on the shared pages index that query_locate's count above depends on."
echo "(query_search shares the identical fix in the same function shape, but a live"
echo "boundary case for it would require fetching extra real distinct URLs into the"
echo "shared production index, permanently growing it and breaking the pages-count"
echo "check above — left to the unit tests instead.)"

echo
echo '== query_select: exactly limit=5 vs. one more row available =='
RESULT=$(call 20 query_select '{"sql":"WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<5) SELECT x FROM cnt","limit":5}')
echo "$RESULT" | grep -Eq '\\?"total\\?":[[:space:]]*5' || fail "expected exactly 5 rows, got: $RESULT"
echo "$RESULT" | grep -Eq '\\?"truncated\\?":[[:space:]]*false' && pass "query_select returning exactly 5 rows (limit=5) is NOT flagged truncated" || fail "false positive at exact cap: $RESULT"

RESULT=$(call 21 query_select '{"sql":"WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<6) SELECT x FROM cnt","limit":5}')
echo "$RESULT" | grep -Eq '\\?"truncated\\?":[[:space:]]*true' && pass "query_select with 6 available rows and limit=5 IS flagged truncated" || fail "expected truncated:true, got: $RESULT"

echo
echo "ALL 13 DEPLOYED TOOLS + boundary regression PASSED against $DOMAIN"
echo "(crawl tier — 6 more tools — is not enabled on this deployment; see server logs / MCP_TIER_CRAWL)"
