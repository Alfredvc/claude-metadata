---
name: extract-conversations
description: Extract structured conversation metadata from Claude Code logs for process improvement analysis. Use when the user wants to analyze collaboration patterns, find failure modes, review skill usage, or improve their workflow with Claude.
user-invocable: true
argument-hint: [project_dir]
allowed-tools: Bash(python3 *), Bash(jq *), Read, Grep, Glob
---

Extract conversation data and save to a temporary file for analysis:

```bash
python3 scripts/extract_session_data.py [project_dir] > /tmp/conversations.json
```

## Output Schema

```json
{
  "project": "string — project path, or \"ALL\" when --all is used",
  "generated_at": "string — ISO 8601 UTC timestamp",
  "conversation_count": "number",
  "session_count": "number",
  "conversations": [
    {
      "slug": "string | null — conversation topic, used to group sessions. null for ungrouped sessions",
      "sessions": [
        {
          "session_id": "string — unique session identifier",
          "file": "string — absolute path to the source JSONL file",
          "is_continuation": "boolean — true if this session continues a previous one (compaction restart)",
          "version": "string | null — Claude Code version",
          "git_branch": "string | null — active git branch during the session",
          "start": "string | null — ISO 8601 timestamp of first entry",
          "end": "string | null — ISO 8601 timestamp of last entry",
          "turns": [
            {
              "index": "number — zero-based turn index within the session",
              "uuid": "string | null — unique turn identifier from the log entry",
              "timestamp": "string | null — ISO 8601 timestamp of the user message",
              "duration_seconds": "number | null — wall-clock seconds from user message to end of Claude's response",
              "user_text": "string — the user's message text",
              "interrupted": "boolean — true if the user interrupted Claude's response",
              "post_interrupt_text": "string — (only present if interrupted) the user's message after interrupting",
              "estimated_input_tokens": "number — (only present for interrupted turns with no API response) estimated from nearest turn with token data",
              "model": "string | null — primary model used (e.g. \"claude-sonnet-4-6\")",
              "api_calls": "number — deduplicated API call count for this turn",
              "tokens": {
                "input": "number — uncached input tokens",
                "cache_read": "number — tokens read from cache",
                "cache_write": "number — tokens written to cache",
                "output": "number — output tokens generated"
              },
              "tools": "object — {ToolName: count} map, e.g. {\"Edit\": 3, \"Bash\": 1}. Empty object if no tools used",
              "skills": [
                {
                  "name": "string — skill name",
                  "source": "string — \"cli\" (user typed /skill) or \"model\" (Claude invoked via Skill tool)"
                }
              ],
              "errors": ["string — error messages from bash failures and tool errors"],
              "subagents": [
                {
                  "agent_id": "string | null — agent identifier",
                  "description": "string — short description from the Agent tool call",
                  "completed_in_turn": "number | null — turn index where the agent completed, null if same turn",
                  "file": "string | null — absolute path to the subagent's JSONL log file",
                  "model": "string | null — primary model used by the subagent",
                  "api_calls": "number — subagent's API call count",
                  "duration_seconds": "number | null — subagent wall-clock duration",
                  "tokens": {
                    "input": "number",
                    "cache_read": "number",
                    "cache_write": "number",
                    "output": "number"
                  },
                  "tools": "object — {ToolName: count} map",
                  "skills": [{"name": "string", "source": "string"}],
                  "errors": ["string"]
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

**Conditional fields:** `post_interrupt_text` is only present on interrupted turns. `estimated_input_tokens` is only present on turns interrupted before any API response. `subagents` is only present on turns that launched agents. All other fields are always present.

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
