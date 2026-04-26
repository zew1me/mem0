#!/usr/bin/env bash
# Wrapper: resolves the repo root relative to this script and delegates to
# server/mcp-start.sh. This lets .mcp.json reference a stable path via
# ${CLAUDE_PLUGIN_ROOT}/scripts/start-mcp.sh without hardcoding the repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec "$REPO_ROOT/server/mcp-start.sh"
