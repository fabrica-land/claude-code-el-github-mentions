#!/bin/bash
# github-mentions — GitHub PR/issue mention watcher for Claude Code.
#
# Community event source for claude-code-event-listeners.
# Install: claude plugin marketplace add mividtim/claude-code-el-github-mentions
#          claude plugin install el-github-mentions
# Or manually: /el:register ./sources.d/github-mentions.sh
#
# Connects to the persistent github-mentions-server.py via HTTP. If the
# server is not running, starts it in the background and waits for it.
# Long-polls for mentions, outputs each as a JSONL line, then exits.
#
# Args: --repo owner/name [--keyword claude] [--interval 30] [--port 7890]
#
# Event Source Protocol:
#   Blocks until a new PR/issue comment mentioning the keyword appears.
#   Outputs JSONL: one JSON object per line.
#   {"id":12345,"type":"issue_comment","pr_number":42,"author":"user","body":"...","html_url":"...","created_at":"..."}

set -euo pipefail

# Resolve through symlinks so companion files are found when registered via el
SCRIPT_DIR="$(cd "$(dirname "$(readlink "$0" 2>/dev/null || echo "$0")")" && pwd)"

# Defaults
REPO=""
KEYWORD="claude"
INTERVAL=30
PORT=7890

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --keyword) KEYWORD="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) REPO="$1"; shift ;;  # positional: repo
  esac
done

if [ -z "$REPO" ]; then
    echo "Usage: github-mentions.sh --repo <owner/repo> [--keyword <word>] [--interval <seconds>] [--port <port>]" >&2
    exit 1
fi

# Export env vars for the server
export GITHUB_MENTIONS_REPO="$REPO"
export GITHUB_MENTIONS_KEYWORD="$KEYWORD"
export GITHUB_MENTIONS_POLL_INTERVAL="$INTERVAL"

SERVER_URL="http://localhost:${PORT}"

# --- Check if the persistent server is running ---
server_healthy() {
    curl -sf "${SERVER_URL}/health" >/dev/null 2>&1
}

if ! server_healthy; then
    # Start the persistent server in the background
    python3 "$SCRIPT_DIR/github-mentions-server.py" "$PORT" </dev/null >/dev/null 2>&1 &
    SERVER_PID=$!

    # Wait up to 5 seconds for the server to come up
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if server_healthy; then
            break
        fi
        sleep 0.5
    done

    if ! server_healthy; then
        echo "ERROR: github-mentions-server.py failed to start on port $PORT" >&2
        exit 1
    fi
fi

# --- Long-poll for mentions ---
RESPONSE=$(curl -sf "${SERVER_URL}/mentions?wait=true" 2>/dev/null) || {
    echo "ERROR: Failed to fetch mentions from github-mentions-server" >&2
    exit 1
}

# Parse JSON array and output each mention as a JSONL line
python3 -c "
import json, sys
try:
    mentions = json.loads(sys.argv[1])
    for m in mentions:
        print(json.dumps(m), flush=True)
except Exception:
    pass
" "$RESPONSE"
