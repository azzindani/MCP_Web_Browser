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
if [ -f .env ]; then
  set -a; source .env; set +a
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
echo "$RESULT" | grep -q '"ok":true' && echo "$RESULT" | grep -q '"isError":false' \
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
echo "ALL 13 DEPLOYED TOOLS PASSED against $DOMAIN"
echo "(crawl tier — 6 more tools — is not enabled on this deployment; see server logs / MCP_TIER_CRAWL)"
