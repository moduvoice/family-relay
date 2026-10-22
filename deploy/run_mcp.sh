#!/bin/bash
# Lambda Web Adapter entry: the Family Relay MCP server on :8080
exec python -m uvicorn relay.mcp_app:app --host 0.0.0.0 --port 8080
