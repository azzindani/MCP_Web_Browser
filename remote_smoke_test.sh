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

echo
echo '== prompt: "fetch https://example.com and index it" -> browse_fetch =='
RESULT=$(curl -s -X POST "$DOMAIN/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $KEY" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"browse_fetch","arguments":{"url":"https://example.com"}}}')
echo "$RESULT" | grep -q '"ok":true' && echo "$RESULT" | grep -q '"isError":false' \
  && pass "browse_fetch(example.com) fetched + indexed the real page" || fail "unexpected result: $RESULT"

echo
echo '== prompt: "what time is it?" -> browse_datetime =='
RESULT=$(curl -s -X POST "$DOMAIN/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $KEY" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"browse_datetime","arguments":{}}}')
echo "$RESULT" | grep -q 'isError.:false' && pass "browse_datetime succeeded" || fail "unexpected result: $RESULT"

echo
echo "ALL CHECKS PASSED against $DOMAIN"
