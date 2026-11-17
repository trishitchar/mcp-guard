"""Minimal stdio MCP server for demos (no SDK needed).

Run directly to test:  python examples/demo_stdio_server.py
Add to Claude Desktop: mcp-guard install --add-demo
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_tools import handle  # noqa: E402


def main() -> None:
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(msg, list):
                replies = [r for r in (handle(m) for m in msg) if r]
                reply = replies or None
            else:
                reply = handle(msg)
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode() + b"\n")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
