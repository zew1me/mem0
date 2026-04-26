#!/usr/bin/env bash
# Hook: Codex PreToolUse (matcher: Edit|Write)
#
# Blocks writes to local memory files and redirects memory persistence to mem0.

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // ""' 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

case "$FILE_PATH" in
  */MEMORY.md|*/memory/*.md|*/.claude/*/memory/*|*/.codex/*/memory/*)
    echo "BLOCKED: Do not write to $FILE_PATH. Use the mem0 MCP \`add_memory\` tool instead to persist memories. This project uses mem0 for memory storage." >&2
    exit 2
    ;;
  *)
    exit 0
    ;;
esac
