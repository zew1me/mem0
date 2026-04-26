#!/usr/bin/env bash
# Wrapper for the mem0 local MCP server.
# Ensures colima and the docker-compose stack are running before proxying
# stdio <-> HTTP so Claude Code can connect at startup without manual setup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEM0_URL="http://localhost:8888"
MEM0_API_KEY="${MEM0_API_KEY:-m0sk_JeL6y6jAVUKVutIiKjlLq6ZptO0Kuz-qmVz0HpyzHNM}"

# Start colima if not running
if ! /opt/homebrew/bin/colima status 2>/dev/null | grep -q "Running"; then
  /opt/homebrew/bin/colima start >/dev/null 2>&1
fi

# Start the mem0 stack if not already responding
if ! curl -sf "${MEM0_URL}/api/health" >/dev/null 2>&1; then
  /opt/homebrew/bin/docker compose -f "${SCRIPT_DIR}/docker-compose.yaml" up -d >/dev/null 2>&1

  # Wait up to 90s for the server to be ready
  for i in $(seq 1 90); do
    if curl -sf "${MEM0_URL}/api/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

exec /opt/homebrew/bin/npx -y mcp-remote "${MEM0_URL}/mcp" \
  --header "X-API-Key:${MEM0_API_KEY}"
