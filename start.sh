#!/usr/bin/env bash
set -euo pipefail

# StreamCut Studio — Portable Local Launcher (macOS / Linux)
# Starts the merged Shorts Editor + 24/7 Live Clipper Bot on http://127.0.0.1:8790/
#
# Usage:
#   ./start.sh            # Start unified Studio + Clipper Bot on :8790
#   ./start.sh --stop     # Stop Studio on :8790 cleanly
#   ./start.sh --bot-only # Start standalone Clipper Bot dashboard on :8787

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
PORT="${STREAMCUT_PORT:-8790}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

stop_port() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      echo "Stopping process on port $p ($pids)..."
      kill $pids 2>/dev/null || true
      sleep 1
    fi
  fi
}

case "${1:-}" in
  --stop)
    stop_port "$PORT"
    stop_port 8787
    echo "StreamCut Studio stopped."
    exit 0
    ;;
  --bot-only)
    stop_port 8787
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    cd "$REPO_DIR/clipper_bot"
    exec nice -n 10 "$PYTHON_BIN" bot.py --port 8787
    ;;
esac

stop_port "$PORT"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export PYTHONUNBUFFERED=1

echo "================================================================"
echo "  StreamCut Studio — 24/7 Live Clipper + YouTube Shorts Studio"
echo "================================================================"
echo "  Open in browser:  http://127.0.0.1:${PORT}/"
echo "  Exported Shorts:  ${REPO_DIR}/shorts_studio/exports"
echo "  Raw Bot Clips:    ${REPO_DIR}/clipper_bot/clips_to_edit"
echo "  Stop cleanly:     Press Ctrl+C (or run ./start.sh --stop)"
echo "================================================================"

cd "$REPO_DIR/shorts_studio"
exec nice -n 10 "$PYTHON_BIN" server.py --port "$PORT"
