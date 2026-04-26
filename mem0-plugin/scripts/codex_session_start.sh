#!/usr/bin/env bash
# Hook: Codex SessionStart (matcher: startup|resume|clear)
#
# Output becomes additional Codex context for the session.

set -uo pipefail

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"' 2>/dev/null || echo "startup")

if [ "$SOURCE" = "startup" ]; then
  cat <<'EOF'
## Mem0 Session Bootstrap

You have access to persistent memory via the mem0 MCP tools. Before starting substantive work:

1. Call `search_memories` with a query related to the current project or user request.
2. Review returned memories for relevant prior context.
3. If useful, call `get_memories` to browse stored memories for this user.
EOF

elif [ "$SOURCE" = "resume" ]; then
  cat <<'EOF'
## Mem0 Session Resumed

Refresh relevant persistent memory before continuing:

1. Call `search_memories` with a query related to the current task.
2. Use any relevant memories to avoid repeating prior investigation.
EOF

elif [ "$SOURCE" = "clear" ]; then
  cat <<'EOF'
## Mem0 Session Cleared

Context was cleared. Reload relevant persistent memory before continuing:

1. Call `search_memories` with queries related to the current task.
2. Check for session state or project memories that help restore context.
EOF
fi

exit 0
