"""Interception proxies: stdio (Claude Desktop) and HTTP (Claude Web connectors)."""

import uuid
from typing import Any

from mcp_guard.jsonrpc import RequestInfo


def build_info(msg: dict[str, Any], *, server: str, source: str, transport: str,
               client_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the pop-up shows and the audit log records."""
    req = RequestInfo.from_message(msg)
    return {
        "server": server,
        "method": req.method,
        "target": req.target,
        "arguments": req.arguments,
        "request_id": req.request_id,
        "source": source,
        "transport": transport,
        "client": client_info or {},
    }


def new_approval_id() -> str:
    return uuid.uuid4().hex
