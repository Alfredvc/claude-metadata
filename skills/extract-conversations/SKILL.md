---
name: extract-conversations
description: Extract structured conversation metadata from Claude Code logs for process improvement analysis. Use when the user wants to analyze collaboration patterns, find failure modes, review skill usage, or improve their workflow with Claude.
user-invocable: true
argument-hint: [project_dir]
allowed-tools: Bash, Read, Grep, Glob
---

Extract conversation data and save to a temporary file for analysis:

```bash
python3 ~/.claude/skills/extract-conversations/extract_session_data.py [project_dir] > /tmp/conversations.json
```

The output is JSON structured as: conversations → sessions → turns, including:
- User messages, timestamps, and durations
- Tool usage counts per turn
- Skill invocations (CLI and model-triggered)
- Token usage (input, cache_read, cache_write, output)
- Interruptions with post-interrupt user text
- Errors (bash failures, tool errors)
- Inlined subagent metadata (tools, tokens, skills, errors, duration)
- Session file paths and turn UUIDs for digging deeper into full transcripts

**Useful flags:**
- `--from YYYY-MM-DD` / `--to YYYY-MM-DD` — filter by date range
- `--no-subdirs` — exclude worktrees
- `--all` — scan all projects
- `--session ID` — filter to a specific session

**After extracting**, use `jq` to query the data. Examples:

```bash
# All interrupted turns with post-interrupt text
jq '[.conversations[].sessions[].turns[] | select(.interrupted) | {user_text, post_interrupt_text, duration_seconds}]' /tmp/conversations.json

# Skill usage frequency
jq '[.conversations[].sessions[].turns[].skills[].name] | group_by(.) | map({skill: .[0], count: length}) | sort_by(-.count)' /tmp/conversations.json

# Turns with errors
jq '[.conversations[].sessions[].turns[] | select(.errors | length > 0) | {user_text: .user_text[:80], errors}]' /tmp/conversations.json

# Subagent summary
jq '[.conversations[].sessions[].turns[].subagents[]? | {description, duration_seconds, api_calls, model}]' /tmp/conversations.json
```

**To dig deeper into a specific turn**, use the turn's `uuid` and the session's `file`:

```bash
# Find the full exchange starting from a turn's UUID
jq -c 'select(.uuid == "TARGET_UUID" or .parentUuid == "TARGET_UUID")' SESSION_FILE.jsonl
```
