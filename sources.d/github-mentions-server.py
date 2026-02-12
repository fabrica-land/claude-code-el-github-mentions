"""Persistent GitHub mentions server for claude-code-el-github-mentions plugin.

Polls GitHub API for PR/issue comments mentioning a keyword, buffers them
in SQLite, and serves them via HTTP long-poll. Same architecture as el-slack
persistent server.

Usage: python3 github-mentions-server.py [port]

Env vars:
    GH_TOKEN                        - GitHub token (falls back to `gh auth token`)
    GITHUB_MENTIONS_REPO            - owner/repo to watch (required)
    GITHUB_MENTIONS_KEYWORD         - Keyword filter, case-insensitive (default: claude)
    GITHUB_MENTIONS_DB_PATH         - SQLite database path (default: /tmp/el-github-mentions.db)
    GITHUB_MENTIONS_WATERMARK       - Watermark file path (default: /tmp/el-github-mentions-watermark)
    GITHUB_MENTIONS_POLL_INTERVAL   - Seconds between polls (default: 30)
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Configuration ---

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('GITHUB_MENTIONS_PORT', '7890'))
REPO = os.environ.get('GITHUB_MENTIONS_REPO', '')
KEYWORD = os.environ.get('GITHUB_MENTIONS_KEYWORD', 'claude')
DB_PATH = os.environ.get('GITHUB_MENTIONS_DB_PATH', '/tmp/el-github-mentions.db')
WATERMARK_FILE = os.environ.get('GITHUB_MENTIONS_WATERMARK', '/tmp/el-github-mentions-watermark')
POLL_INTERVAL = int(os.environ.get('GITHUB_MENTIONS_POLL_INTERVAL', '30'))

# --- Token resolution ---

def _resolve_token():
    """Get GitHub token from GH_TOKEN env var or gh CLI."""
    token = os.environ.get('GH_TOKEN', '')
    if token:
        return token
    try:
        result = subprocess.run(
            ['gh', 'auth', 'token'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ''

GH_TOKEN = _resolve_token()

# --- Database ---

_db_lock = threading.Lock()


def _get_db():
    """Create a new connection for the calling thread."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mentions (
            id          INTEGER PRIMARY KEY,
            type        TEXT,
            pr_number   INTEGER,
            author      TEXT,
            body        TEXT,
            path        TEXT,
            html_url    TEXT,
            created_at  TEXT,
            picked_up   INTEGER DEFAULT 0,
            received_at REAL
        )
    """)
    conn.commit()
    return conn


def _init_db():
    """Initialize the database and apply watermark on startup."""
    conn = _get_db()
    watermark = _read_watermark()
    if watermark:
        conn.execute("UPDATE mentions SET picked_up = 1 WHERE created_at <= ?", (watermark,))
        conn.commit()
    conn.close()


def _insert_mention(mention):
    """Insert a mention into the buffer. Returns True if inserted (not a dup)."""
    with _db_lock:
        conn = _get_db()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO mentions "
                "(id, type, pr_number, author, body, path, html_url, created_at, picked_up, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    mention['id'],
                    mention['type'],
                    mention['pr_number'],
                    mention['author'],
                    mention['body'],
                    mention.get('path'),
                    mention['html_url'],
                    mention['created_at'],
                    time.time(),
                ),
            )
            inserted = cursor.rowcount > 0
            conn.commit()
            return inserted
        finally:
            conn.close()


def _pick_mentions():
    """Return all unpicked mentions and mark them as picked up."""
    with _db_lock:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT id, type, pr_number, author, body, path, html_url, created_at "
                "FROM mentions WHERE picked_up = 0 ORDER BY created_at ASC"
            ).fetchall()
            if not rows:
                return []
            mentions = []
            max_ts = ''
            for row in rows:
                mention = {
                    'id': row[0],
                    'type': row[1],
                    'pr_number': row[2],
                    'author': row[3],
                    'body': row[4],
                    'html_url': row[6],
                    'created_at': row[7],
                }
                if row[5]:
                    mention['path'] = row[5]
                mentions.append(mention)
                if row[7] > max_ts:
                    max_ts = row[7]
            # Mark picked up
            id_list = [m['id'] for m in mentions]
            placeholders = ','.join('?' for _ in id_list)
            conn.execute(
                f"UPDATE mentions SET picked_up = 1 WHERE id IN ({placeholders})",
                id_list,
            )
            conn.commit()
            # Advance watermark
            if max_ts:
                _write_watermark(max_ts)
            return mentions
        finally:
            conn.close()


def _pending_count():
    """Count unpicked mentions."""
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute("SELECT COUNT(*) FROM mentions WHERE picked_up = 0").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


# --- Watermark ---

def _read_watermark():
    try:
        with open(WATERMARK_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''


def _write_watermark(ts):
    with open(WATERMARK_FILE, 'w') as f:
        f.write(ts)


# --- Notification for long-poll waiters ---

_waiter_event = threading.Event()


def _notify_waiters():
    """Signal any long-poll waiters that a new mention arrived."""
    _waiter_event.set()


# --- HTTP Handler ---

class MentionsHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Handle GET endpoints: /mentions, /mentions?wait=true, /health."""
        try:
            path = self.path.split('?')[0]
            query = self.path.split('?')[1] if '?' in self.path else ''
            params = {}
            for part in query.split('&'):
                if '=' in part:
                    k, v = part.split('=', 1)
                    params[k] = v
                elif part:
                    params[part] = 'true'

            if path == '/mentions':
                self._handle_mentions(params)
            elif path == '/health':
                self._handle_health()
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
        except Exception as e:
            sys.stderr.write(f"[github-mentions] GET error: {e}\n")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def _handle_mentions(self, params):
        """GET /mentions — return unpicked mentions as JSON array."""
        wait = params.get('wait', '').lower() == 'true'

        if wait:
            # Long-poll: block until a mention arrives
            # 30s timeout, 200ms poll loop, 500ms batch wait after first mention
            deadline = time.time() + 30.0
            while time.time() < deadline:
                if _pending_count() > 0:
                    break
                _waiter_event.clear()
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                _waiter_event.wait(timeout=min(0.2, remaining))

            # If we got a mention, wait 500ms for burst collection
            if _pending_count() > 0:
                time.sleep(0.5)

        mentions = _pick_mentions()
        body = json.dumps(mentions).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        """GET /health — return status and pending count."""
        pending = _pending_count()
        body = json.dumps({"status": "ok", "pending": pending}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


# --- GitHub API Poller ---

def _extract_pr_number(comment, comment_type):
    """Extract PR/issue number from a GitHub comment object."""
    if comment_type == 'issue_comment':
        issue_url = comment.get('issue_url', '')
        # format: https://api.github.com/repos/owner/repo/issues/42
        if issue_url:
            return int(issue_url.rstrip('/').split('/')[-1])
    elif comment_type == 'review_comment':
        pr_url = comment.get('pull_request_url', '')
        # format: https://api.github.com/repos/owner/repo/pulls/42
        if pr_url:
            return int(pr_url.rstrip('/').split('/')[-1])
    return 0


def _github_api_get(url):
    """Make an authenticated GET request to the GitHub API."""
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {GH_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def _poll_comments():
    """Background thread: poll GitHub API for new comments mentioning the keyword."""
    if not REPO:
        sys.stderr.write("[github-mentions] ERROR: GITHUB_MENTIONS_REPO not set, poller disabled\n")
        return
    if not GH_TOKEN:
        sys.stderr.write("[github-mentions] ERROR: No GitHub token available, poller disabled\n")
        return

    sys.stderr.write(f"[github-mentions] Polling {REPO} for '{KEYWORD}' every {POLL_INTERVAL}s\n")

    keyword_lower = KEYWORD.lower()

    while True:
        try:
            watermark = _read_watermark()
            since_param = f"&since={watermark}" if watermark else ""

            new_max_ts = watermark

            # Poll issue comments (conversation tab)
            try:
                issue_url = (
                    f"https://api.github.com/repos/{REPO}/issues/comments"
                    f"?sort=created&direction=asc&per_page=100{since_param}"
                )
                issue_comments = _github_api_get(issue_url)
                for comment in issue_comments:
                    body = comment.get('body', '')
                    if keyword_lower not in body.lower():
                        continue
                    created_at = comment.get('created_at', '')
                    # Skip comments at or before watermark (since= is inclusive)
                    if watermark and created_at <= watermark:
                        continue
                    mention = {
                        'id': comment['id'],
                        'type': 'issue_comment',
                        'pr_number': _extract_pr_number(comment, 'issue_comment'),
                        'author': comment.get('user', {}).get('login', ''),
                        'body': body,
                        'html_url': comment.get('html_url', ''),
                        'created_at': created_at,
                    }
                    if _insert_mention(mention):
                        _notify_waiters()
                    if created_at > (new_max_ts or ''):
                        new_max_ts = created_at
            except Exception as e:
                sys.stderr.write(f"[github-mentions] issue comments poll error: {e}\n")

            # Poll PR review comments (inline code comments)
            try:
                review_url = (
                    f"https://api.github.com/repos/{REPO}/pulls/comments"
                    f"?sort=created&direction=asc&per_page=100{since_param}"
                )
                review_comments = _github_api_get(review_url)
                for comment in review_comments:
                    body = comment.get('body', '')
                    if keyword_lower not in body.lower():
                        continue
                    created_at = comment.get('created_at', '')
                    if watermark and created_at <= watermark:
                        continue
                    mention = {
                        'id': comment['id'],
                        'type': 'review_comment',
                        'pr_number': _extract_pr_number(comment, 'review_comment'),
                        'author': comment.get('user', {}).get('login', ''),
                        'body': body,
                        'path': comment.get('path', ''),
                        'html_url': comment.get('html_url', ''),
                        'created_at': created_at,
                    }
                    if _insert_mention(mention):
                        _notify_waiters()
                    if created_at > (new_max_ts or ''):
                        new_max_ts = created_at
            except Exception as e:
                sys.stderr.write(f"[github-mentions] review comments poll error: {e}\n")

            # Advance watermark even if no new mentions (so we don't re-scan)
            if new_max_ts and new_max_ts != watermark:
                _write_watermark(new_max_ts)

        except Exception as e:
            sys.stderr.write(f"[github-mentions] poll error: {e}\n")

        time.sleep(POLL_INTERVAL)


# --- Main ---

def main():
    if not REPO:
        sys.stderr.write("[github-mentions] ERROR: GITHUB_MENTIONS_REPO is required\n")
        sys.exit(1)
    if not GH_TOKEN:
        sys.stderr.write("[github-mentions] ERROR: No GitHub token found (set GH_TOKEN or install gh CLI)\n")
        sys.exit(1)

    _init_db()

    # Start GitHub API poller in background
    poller = threading.Thread(target=_poll_comments, daemon=True)
    poller.start()

    server = HTTPServer(('0.0.0.0', PORT), MentionsHandler)
    sys.stderr.write(f"[github-mentions] Listening on 0.0.0.0:{PORT}\n")
    sys.stderr.write(f"[github-mentions] DB: {DB_PATH}\n")
    sys.stderr.write(f"[github-mentions] Repo: {REPO}, Keyword: {KEYWORD}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[github-mentions] Shutting down.\n")
        server.shutdown()


if __name__ == '__main__':
    main()
