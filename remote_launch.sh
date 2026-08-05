#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# mcp-web-browser — remote run + tunnel (Google Colab / any fresh Linux VM,
# no Docker needed). Starts the server directly (uv run) and opens a
# Cloudflare Quick Tunnel to it — same idea as azzindani/Folio's launch.sh,
# and this repo's own launch_tunnel.sh (which does the same thing via
# Docker). Use this one when Docker isn't available.
#
# Usage:
#   REPO_DIR=/content/MCP_Web_Browser ./remote_launch.sh
#   ./remote_launch.sh stop
#
# NOT for production. Quick Tunnels are unauthenticated at the transport
# level — set WEB_API_KEY or WEB_TOKENS_FILE before launching so /mcp
# still requires a bearer token even while it's publicly reachable.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="${REPO_DIR:-/content/MCP_Web_Browser}"
PORT="${WEB_PORT:-8766}"
LOG_DIR="/tmp/web-browser-remote"
mkdir -p "$LOG_DIR"

if [ "${1:-}" = "stop" ]; then
  pkill -f "cloudflared tunnel --url http://localhost:${PORT}" 2>/dev/null && echo "tunnel stopped" || echo "no tunnel running"
  pkill -f "python server.py" 2>/dev/null && echo "server stopped" || echo "no server running"
  exit 0
fi

if ! command -v cloudflared &>/dev/null; then
  echo "[remote_launch] installing cloudflared..."
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
  chmod +x /usr/local/bin/cloudflared
fi
export PATH="${HOME}/.local/bin:${PATH}"

pkill -f "cloudflared tunnel --url http://localhost:${PORT}" 2>/dev/null || true
pkill -f "python server.py" 2>/dev/null || true
sleep 1

cd "$REPO_DIR"
echo "[remote_launch] starting mcp-web-browser on :${PORT}..."
nohup uv run python server.py --transport http --host 0.0.0.0 --port "$PORT" > "$LOG_DIR/server.log" 2>&1 &

for i in $(seq 1 30); do
  curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done

echo "[remote_launch] starting cloudflared quick tunnel..."
: > "$LOG_DIR/tunnel.log"
nohup cloudflared tunnel --url "http://localhost:${PORT}" > "$LOG_DIR/tunnel.log" 2>&1 &

URL=""
for i in $(seq 1 30); do
  URL=$(grep -oP 'https://[a-z0-9\-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" 2>/dev/null | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -n "$URL" ]; then
  echo ""
  echo "  $URL/mcp"
  echo ""
else
  echo "Tunnel URL not found — check $LOG_DIR/tunnel.log"
  tail -20 "$LOG_DIR/tunnel.log"
fi

echo "  stop:  ./remote_launch.sh stop"
