# claude-api-usage

## **Tools for analyzing your Claude Code usage**

![Example output](docs/output.png)

---

## Install as Claude Code plugin

Requires Claude Code 1.0.33+.

### From marketplace (snapshot)

```sh
/plugin install claude-api-usage
```

### Local development (live-linked)

Register the repo as a local marketplace, install, then replace the cached copy with a symlink so edits are reflected immediately:

```sh
claude plugin marketplace add /path/to/claude-api-usage
claude plugin install claude-api-usage@claude-api-usage

# Replace the cache copy with a symlink to the source repo
rm -rf ~/.claude/plugins/cache/claude-api-usage/claude-api-usage/1.0.0
ln -s /path/to/claude-api-usage ~/.claude/plugins/cache/claude-api-usage/claude-api-usage/1.0.0
```

Skills are namespaced under the plugin name:
- `claude-api-usage:api-usage`
- `claude-api-usage:extract-conversations`

---

## Skills

### `api-usage` — Token usage & cost breakdown

Curious what your Claude Code usage would cost at API prices?

- Per-model breakdown — input, cache write, cache read, and output tokens
- Cost matrix across all models and token types
- Cache efficiency stats — hit rate and how much caching saved you
- Time summary with average monthly cost and per-model projections
- Multi-project support — single project, worktrees, or everything at once
- Parallel processing for large log sets

```sh
python3 skills/api-usage/api_usage.py [project_dir] [--no-subdirs] [--all]
```

| Argument | Description |
|---|---|
| `project_dir` | Path to the project root (default: current directory) |
| `--no-subdirs` | Exclude worktrees and sub-directories from the scan |
| `--all` | Scan every Claude project — your grand total across everything |

### `extract-conversations` — Conversation data for process analysis

Extract structured conversation data to analyze collaboration patterns, find failure modes, and improve workflows.

Outputs JSON structured as **conversations → sessions → turns**, including:
- User messages, timestamps, and durations
- Tool usage counts and skill invocations (CLI vs model-triggered)
- Token usage per turn and per subagent
- Interruptions with post-interrupt user text
- Errors (bash failures, tool errors)
- Inlined subagent metadata
- File paths and UUIDs for digging deeper

```sh
python3 skills/extract-conversations/extract_session_data.py [project_dir] [options]
```

| Argument | Description |
|---|---|
| `project_dir` | Path to the project root (default: current directory) |
| `--no-subdirs` | Exclude worktrees and sub-directories |
| `--all` | Scan every Claude project |
| `--from YYYY-MM-DD` | Filter sessions starting from this date |
| `--to YYYY-MM-DD` | Filter sessions up to this date |
| `--session ID` | Filter to a specific session |

**Example: extract and query with jq**

```sh
# Extract to file
python3 skills/extract-conversations/extract_session_data.py ~/src/my-project > /tmp/conversations.json

# Find all interrupted turns
jq '[.conversations[].sessions[].turns[] | select(.interrupted)]' /tmp/conversations.json

# Skill usage frequency
jq '[.conversations[].sessions[].turns[].skills[].name] | group_by(.) | map({skill: .[0], count: length}) | sort_by(-.count)' /tmp/conversations.json
```

---

## Requirements

- Python 3.10+
- Claude Code 1.0.33+
