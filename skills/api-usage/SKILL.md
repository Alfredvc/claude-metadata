---
name: api-usage
description: Show Claude API token usage and cost breakdown for a project. Use when the user asks about API costs, spending, token usage, or how much Claude Code has cost.
user-invocable: true
argument-hint: [project_dir]
allowed-tools: Bash
---

Run the api-usage script for the given project directory (default: current working directory):

```bash
python3 ~/.claude/skills/api-usage/api_usage.py [project_dir]
```

- If the user doesn't specify a directory, use the current project root.
- Pass `--no-subdirs` if the user only wants the main project, not worktrees or sub-dirs.
- Pass `--all` to scan every Claude project at once.
- Present the output as-is — it's already formatted for the terminal.
