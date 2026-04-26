# Local FastAPI-MCP integration

When exposing the local `server/` FastAPI app as MCP, add `fastapi-mcp>=0.4.0,<1.0.0`, import `FastApiMCP`, give selected routes explicit `operation_id`s, and mount after route registration with `include_operations` plus `headers=["authorization", "x-api-key"]`. This preserves existing REST auth for MCP tool calls and exposes clean tool names at `/mcp`.

Verified on 2026-04-25 with `docker compose up -d --build mem0`, `make health`, MCP `initialize`, `tools/list`, and an unauthenticated `tools/call` returning the expected 401.