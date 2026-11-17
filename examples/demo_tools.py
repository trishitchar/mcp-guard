"""Demo tools. Every executed call is logged to demo_executions.log, so a blocked
call visibly never reached the server."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mcp-guard-demo", "version": "1.0.0"}


def _home() -> Path:
    base = Path(os.environ.get("MCP_GUARD_HOME") or Path.home() / ".mcp_guard")
    base.mkdir(parents=True, exist_ok=True)
    return base


NOTES_DIR = _home() / "demo_notes"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_time",
        "description": "Return the current local date and time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "echo",
        "description": "Echo a message back.",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
    {
        "name": "write_note",
        "description": "Save a text note to disk.",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
            "required": ["title", "content"],
        },
    },
    {
        "name": "list_notes",
        "description": "List saved notes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_all_notes",
        "description": "Permanently delete every saved note.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _log_execution(name: str, args: dict[str, Any]) -> None:
    line = f"{_dt.datetime.now().isoformat(timespec='seconds')} EXECUTED {name} {json.dumps(args)}"
    print(f"[demo-server] {line}", file=sys.stderr, flush=True)
    with open(_home() / "demo_executions.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    _log_execution(name, args)
    if name == "get_time":
        text = _dt.datetime.now().strftime("%A %d %B %Y, %H:%M:%S")
    elif name == "echo":
        text = str(args.get("message", ""))
    elif name == "write_note":
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_\- ]", "_", str(args.get("title", "note")))[:60] or "note"
        path = NOTES_DIR / f"{safe}.txt"
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        text = f"Saved note to {path}"
    elif name == "list_notes":
        notes = sorted(p.name for p in NOTES_DIR.glob("*.txt")) if NOTES_DIR.exists() else []
        text = "\n".join(notes) or "(no notes)"
    elif name == "delete_all_notes":
        count = 0
        for p in NOTES_DIR.glob("*.txt") if NOTES_DIR.exists() else []:
            p.unlink()
            count += 1
        text = f"Deleted {count} note(s)"
    else:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Returns the response, or None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if "id" not in msg:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}
