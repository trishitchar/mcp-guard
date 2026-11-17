"""JSON-RPC / MCP message helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

ERROR_BLOCKED = -32001  # server-defined error range


def parse(raw: bytes | str) -> Any | None:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def is_gated(msg: Any, gated_methods: list[str]) -> bool:
    return is_request(msg) and msg.get("method") in gated_methods


@dataclass
class RequestInfo:
    method: str
    target: str  # tool name / resource uri / prompt name
    arguments: Any = field(default_factory=dict)
    request_id: Any = None

    @classmethod
    def from_message(cls, msg: dict[str, Any]) -> "RequestInfo":
        method = str(msg.get("method", ""))
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {"_params": params}
        if method == "tools/call":
            target = str(params.get("name", "<unnamed tool>"))
            args = params.get("arguments", {})
        elif method == "resources/read":
            target = str(params.get("uri", "<no uri>"))
            args = {k: v for k, v in params.items() if k != "uri"}
        elif method == "prompts/get":
            target = str(params.get("name", "<unnamed prompt>"))
            args = params.get("arguments", {})
        else:
            target = method
            args = params
        return cls(method=method, target=target, arguments=args, request_id=msg.get("id"))


def dumps(msg: Any) -> bytes:
    """One line, no newlines (stdio framing)."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def denial_response(msg: dict[str, Any], reason: str, style: str = "tool_error") -> dict:
    """Reply for a blocked request. tools/call gets an isError tool result by
    default so Claude sees the reason; everything else gets a JSON-RPC error."""
    request_id = msg.get("id")
    text = f"Request blocked by MCP Guard: {reason}"
    if msg.get("method") == "tools/call" and style == "tool_error":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": True,
            },
        }
    return error_response(request_id, ERROR_BLOCKED, text, {"blockedBy": "mcp-guard"})
