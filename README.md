# claude-code-el-github-mentions

Community event source for [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) that watches GitHub PR and issue comments for keyword mentions.

Replaces the one-shot `pr-mentions.sh` with a persistent server using SQLite buffering — no missed comments between restarts.

## Install

```bash
# From the marketplace (recommended — auto-discovers, pulls in el as dependency)
claude plugin marketplace add mividtim/claude-code-el-github-mentions
claude plugin install el-github-mentions

# Or manually register the source
git clone https://github.com/mividtim/claude-code-el-github-mentions.git
/el:register ./claude-code-el-github-mentions/sources.d/github-mentions.sh
```

## Prerequisites

- **GitHub token**: `gh` CLI authenticated, or `GH_TOKEN` env var set
- Python 3
- curl

## Architecture

```
GitHub API <-- [persistent server :PORT polls] --> SQLite buffer
                                                        |
              agent --> [github-mentions.sh] --> HTTP GET /mentions?wait=true
                                                        |
                                                        v
                                                  el plugin (JSONL)
```

The persistent server (`github-mentions-server.py`) runs in the background, polling the GitHub API at a configurable interval. Comments matching the keyword are buffered in SQLite. The shell script (`github-mentions.sh`) long-polls the server for new mentions and outputs them as JSONL.

Benefits over the legacy `pr-mentions.sh`:
- **No missed comments** — SQLite buffer persists across restarts
- **Watermark advances** — tracks last-seen timestamp, never re-processes
- **Burst collection** — multiple comments arriving together are batched
- **Long-poll** — efficient blocking instead of sleep loops
- **Both comment types** — issue comments and inline review comments
- **100 per page** — handles bursts better than the old 50-item limit

## Usage

```
/el:listen github-mentions --repo owner/name
```

With options:

```
/el:listen github-mentions --repo owner/name --keyword claude --interval 30 --port 7890
```

## Server Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/mentions` | GET | Returns unpicked mentions as a JSON array, marks them as picked up, and advances the watermark. |
| `/mentions?wait=true` | GET | Long-poll: blocks up to 30s until a mention arrives. Waits 500ms after the first to collect bursts. |
| `/health` | GET | Returns `{"status":"ok","pending":N}` where N is the count of unpicked mentions. |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GH_TOKEN` | *(from `gh auth token`)* | GitHub token for API access |
| `GITHUB_MENTIONS_REPO` | *(required)* | `owner/repo` to watch |
| `GITHUB_MENTIONS_KEYWORD` | `claude` | Case-insensitive keyword filter |
| `GITHUB_MENTIONS_DB_PATH` | `/tmp/el-github-mentions.db` | SQLite buffer database |
| `GITHUB_MENTIONS_WATERMARK` | `/tmp/el-github-mentions-watermark` | Last-seen timestamp file |
| `GITHUB_MENTIONS_POLL_INTERVAL` | `30` | Seconds between GitHub API polls |
| `GITHUB_MENTIONS_PORT` | `7890` | Server port |

## Output Format

```json
{"id":12345,"type":"issue_comment","pr_number":42,"author":"timgarthwaite","body":"@claude please review","html_url":"https://github.com/owner/repo/issues/42#issuecomment-12345","created_at":"2026-02-12T10:30:00Z"}
```

Review comments include `path` (the file path):

```json
{"id":67890,"type":"review_comment","pr_number":42,"author":"timgarthwaite","body":"@claude check this","path":"src/main.py","html_url":"https://github.com/owner/repo/pull/42#discussion_r67890","created_at":"2026-02-12T10:31:00Z"}
```

When multiple mentions are buffered, each is output on its own line (JSONL format).

## What it handles

| Concern | How |
|---------|-----|
| **Keyword matching** | Case-insensitive search in comment body |
| **Deduplication** | GitHub comment ID as SQLite primary key (`INSERT OR IGNORE`) |
| **Watermark** | ISO timestamp persisted to file, advances on drain |
| **Both comment types** | Issue comments (conversation) and PR review comments (inline) |
| **Burst collection** | 500ms batch window collects rapid-fire comments |
| **No restart gap** | SQLite buffer persists — nothing lost between listener restarts |
| **Token resolution** | Tries `gh auth token` first, falls back to `GH_TOKEN` env var |

## Migration from pr-mentions.sh

Replace:
```
event-listen.sh pr-mentions --repo fabrica-land/fabrica-v3-api --interval 30
```
With:
```
event-listen.sh github-mentions --repo fabrica-land/fabrica-v3-api --interval 30
```

## Requirements

- [claude-code-event-listeners](https://github.com/mividtim/claude-code-event-listeners) plugin installed
- Python 3
- curl
- `gh` CLI authenticated or `GH_TOKEN` env var

## License

MIT
