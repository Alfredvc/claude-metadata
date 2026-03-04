#!/bin/sh
set -e

CLAUDE_DIR="$HOME/.claude"
if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Error: Claude directory not found at $CLAUDE_DIR" >&2
    echo "Please install Claude Code first: https://claude.ai/download" >&2
    exit 1
fi

DIR="$CLAUDE_DIR/skills/api-usage"
mkdir -p "$DIR"

echo "Downloading skill files..."

if ! curl -fsSL https://raw.githubusercontent.com/Alfredvc/claude-api-usage/refs/heads/main/skill/SKILL.md -o "$DIR/SKILL.md"; then
    echo "Error: Failed to download SKILL.md" >&2
    exit 1
fi

if ! curl -fsSL https://raw.githubusercontent.com/Alfredvc/claude-api-usage/refs/heads/main/skill/api_usage.py -o "$DIR/api_usage.py"; then
    echo "Error: Failed to download api_usage.py" >&2
    exit 1
fi

chmod +x "$DIR/api_usage.py"

echo "Installed. Use /api-usage in Claude Code."
