"""Reverse proxy for remote (Streamable HTTP) MCP servers: the Claude Web path.

Claude Web's MCP calls come from Anthropic's cloud, not this machine, so we sit
in front of the server and register the proxy's URL as the connector:

    claude.ai -> tunnel -> HttpGuardProxy -> real MCP server

Only POST bodies are inspected; responses (JSON or SSE) are streamed back as-is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from mcp_guard import jsonrpc
from mcp_guard.client import DaemonClient, DaemonUnavailable, Decision
from mcp_guard.config import Settings
from mcp_guard.proxy import build_info, new_approval_id

log = logging.getLogger("mcp_guard.http")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}


def _describe_http_client(request: web.Request, client_info: dict[str, Any]) -> str:
    ua = request.headers.get("User-Agent", "")
    name = client_info.get("name", "")
    if "claude" in ua.lower() or "claude" in str(name).lower() or "anthropic" in ua.lower():
        return f"Claude (remote connector - Web/Desktop) [{name or ua[:40]}]"
    return f"HTTP client [{name or ua[:40] or request.remote}]"


class HttpGuardProxy:
    def __init__(self, upstream: str, server_name: str, settings: Settings,
                 client: DaemonClient | None = None, path_secret: str | None = None) -> None:
        self.upstream = upstream.rstrip("/")
        self.name = server_name
        self.settings = settings
        self.client = client or DaemonClient(settings)
        self.path_secret = path_secret
        self.client_info: dict[str, dict[str, Any]] = {}  # Mcp-Session-Id -> clientInfo
        self._session: ClientSession | None = None

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=20 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _app: web.Application) -> None:
        # No total timeout: SSE streams can stay open.
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=15),
                                      auto_decompress=False)

    async def _on_cleanup(self, _app: web.Application) -> None:
        if self._session:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        if self.path_secret:
            prefix = f"/{self.path_secret}"
            if not (path == prefix or path.startswith(prefix + "/")):
                return web.json_response({"error": "not found"}, status=404)
            path = path[len(prefix):] or "/"

        body = await request.read()
        extra_denials: list[dict[str, Any]] = []

        if request.method == "POST" and body:
            msg = jsonrpc.parse(body)
            gated = self.settings.gated_methods
            session_id = request.headers.get("Mcp-Session-Id", "")

            if isinstance(msg, dict) and msg.get("method") == "initialize":
                info = (msg.get("params") or {}).get("clientInfo") or {}
                self.client_info["pending-init"] = info
                self.client.send_event("client_connected", {
                    "server": self.name, "transport": "http",
                    "source": _describe_http_client(request, info), "detail": request.remote or "",
                })

            if isinstance(msg, dict) and jsonrpc.is_gated(msg, gated):
                decision = await self._approve(msg, request, session_id)
                if not decision.allowed:
                    denial = jsonrpc.denial_response(msg, decision.reason, self.settings.block_response)
                    return web.json_response(denial)

            elif isinstance(msg, list) and any(jsonrpc.is_gated(m, gated) for m in msg):
                idx = [i for i, m in enumerate(msg) if jsonrpc.is_gated(m, gated)]
                decisions = await asyncio.gather(*(self._approve(msg[i], request, session_id) for i in idx))
                blocked = {i for i, d in zip(idx, decisions) if not d.allowed}
                extra_denials = [jsonrpc.denial_response(msg[i], d.reason, self.settings.block_response)
                                 for i, d in zip(idx, decisions) if not d.allowed]
                remaining = [m for i, m in enumerate(msg) if i not in blocked]
                if not remaining:
                    return web.json_response(extra_denials)
                body = jsonrpc.dumps(remaining)

        return await self._forward(request, path, body, extra_denials)

    async def _approve(self, msg: dict[str, Any], request: web.Request, session_id: str) -> Decision:
        client_info = self.client_info.get(session_id) or self.client_info.get("pending-init") or {}
        info = build_info(msg, server=self.name, source=_describe_http_client(request, client_info),
                          transport="http", client_info=client_info)
        approval_id = new_approval_id()
        try:
            return await asyncio.to_thread(self.client.request_approval, approval_id, info)
        except asyncio.CancelledError:  # client disconnected: close the pop-up
            await asyncio.to_thread(self.client.cancel, approval_id, "remote client disconnected")
            raise
        except DaemonUnavailable as exc:
            log.error("Approval daemon unavailable: %s", exc)
            if self.settings.fail_mode == "open":
                return Decision("allow", "daemon unavailable (fail-open)", "fail-open")
            return Decision("block", "the MCP Guard approval app is not running on the user's computer.",
                            "fail-closed")

    async def _forward(self, request: web.Request, path: str, body: bytes,
                       extra_denials: list[dict[str, Any]]) -> web.StreamResponse:
        assert self._session is not None
        url = self.upstream + path + (f"?{request.query_string}" if request.query_string else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            upstream = await self._session.request(
                request.method, url, headers=headers, data=body or None, allow_redirects=False
            )
        except Exception as exc:
            log.error("Upstream %s unreachable: %s", url, exc)
            return web.json_response(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32603, "message": f"MCP Guard: upstream server unreachable ({exc})"}},
                status=502,
            )

        async with upstream:
            if upstream.headers.get("Mcp-Session-Id") and "pending-init" in self.client_info:
                self.client_info[upstream.headers["Mcp-Session-Id"]] = self.client_info.pop("pending-init")

            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            ctype = upstream.headers.get("Content-Type", "")

            if extra_denials and ctype.startswith("application/json"):
                # Add denials for blocked batch items to the JSON array.
                data = await upstream.read()
                try:
                    parsed = json.loads(data)
                    merged = (parsed if isinstance(parsed, list) else [parsed]) + extra_denials
                    data = jsonrpc.dumps(merged)
                except ValueError:
                    pass
                resp_headers.pop("Content-Encoding", None)
                return web.Response(status=upstream.status, body=data, headers=resp_headers)

            resp = web.StreamResponse(status=upstream.status, headers=resp_headers)
            await resp.prepare(request)
            if extra_denials and ctype.startswith("text/event-stream"):
                for d in extra_denials:
                    await resp.write(b"event: message\ndata: " + jsonrpc.dumps(d) + b"\n\n")
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp


def run_http_proxy(upstream: str, server_name: str, settings: Settings, host: str, port: int,
                   path_secret: str | None) -> int:
    logging.basicConfig(level=logging.INFO, format="[mcp-guard:http] %(levelname)s %(message)s")
    if path_secret == "auto":
        path_secret = secrets.token_urlsafe(18)
    proxy = HttpGuardProxy(upstream, server_name, settings, path_secret=path_secret)
    prefix = f"/{path_secret}" if path_secret else ""
    print(f"MCP Guard HTTP proxy for '{server_name}'")
    print(f"  upstream : {upstream}")
    print(f"  listening: http://{host}:{port}{prefix}/  (expose this via an HTTPS tunnel for Claude Web)")
    if not DaemonClient(settings).health():
        print("  WARNING  : approval daemon not reachable - requests will be "
              + ("allowed (fail-open)" if settings.fail_mode == "open" else "BLOCKED (fail-closed)"))

    async def _main() -> None:
        runner = web.AppRunner(proxy.make_app(), handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0
