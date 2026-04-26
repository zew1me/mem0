#!/usr/bin/env bash
# Hook: Codex UserPromptSubmit
#
# Searches mem0 for memories relevant to the submitted prompt and emits
# additional Codex context. This hook must never block the user's prompt.

set -uo pipefail

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""' 2>/dev/null || echo "")

if [ ${#PROMPT} -lt 20 ]; then
  exit 0
fi

API_KEY="${MEM0_API_KEY:-}"
if [ -z "$API_KEY" ]; then
  exit 0
fi

BASE_URL="${MEM0_LOCAL_URL:-http://localhost:8888}"
USER_ID="${MEM0_USER_ID:-${USER:-default}}"

BODY=$(jq -n --arg query "$PROMPT" --arg user_id "$USER_ID" \
  '{query: $query, user_id: $user_id, top_k: 5}')

RESPONSE=$(curl -s --max-time 3 \
  -X POST "${BASE_URL}/search" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$BODY" \
  2>/dev/null || echo "")

if [ -z "$RESPONSE" ]; then
  exit 0
fi

MEMORIES=$(echo "$RESPONSE" | jq -r '
  if type == "array" then . else .results // [] end |
  if length == 0 then empty else
  "## Relevant memories from mem0\n\n" +
  (map(select(.memory != null) | "- " + .memory) | join("\n"))
  end
' 2>/dev/null || echo "")

if [ -n "$MEMORIES" ]; then
  echo "$MEMORIES"
fi

exit 0
