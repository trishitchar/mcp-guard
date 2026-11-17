"""Minimal Streamable-HTTP MCP server for the Claude Web demo (no SDK needed).

    python examples/demo_http_server.py --port 9000
    mcp-guard http --upstream http://127.0.0.1:9000 --name demo-remote --port 8787 --path-secret auto
    cloudflared tunnel --url http://127.0.0.1:8787     # gives an https://....trycloudflare.com URL

Then add ``https://<tunnel>/<secret>/mcp`` as a custom connector in claude.ai.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_tools import handle  # noqa: E402


async def mcp_post(request: web.Request) -> web.Response:
    try:
        msg = json.loads(await request.read())
    except ValueError:
        return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                                 status=400)
    headers = {}
    if isinstance(msg, dict) and msg.get("method") == "initialize":
        headers["Mcp-Session-Id"] = uuid.uuid4().hex
    if isinstance(msg, list):
        replies = [r for r in (handle(m) for m in msg) if r]
        return web.json_response(replies, headers=headers) if replies else web.Response(status=202)
    reply = handle(msg)
    if reply is None:
        return web.Response(status=202)
    return web.json_response(reply, headers=headers)


async def mcp_get(_request: web.Request) -> web.Response:
    return web.Response(status=405, text="SSE stream not supported by demo server")


async def mcp_delete(_request: web.Request) -> web.Response:
    return web.Response(status=200)


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/mcp", mcp_post)
    app.router.add_get("/mcp", mcp_get)
    app.router.add_delete("/mcp", mcp_delete)
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    a = ap.parse_args()
    web.run_app(make_app(), host=a.host, port=a.port)
