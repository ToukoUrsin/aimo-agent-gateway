#!/bin/bash
# Start the gateway, the demo console and the MCP endpoint on one port.
cd "$(dirname "$0")"
exec uv run -q uvicorn gateway.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8642}"
