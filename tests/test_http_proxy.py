"""HTTP (remote connector / Claude Web) proxy against the demo HTTP MCP server."""

from __future__ import annotations

import asyncio

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from conftest import decider_from, executions
from demo_http_server import make_app as make_upstream
from mcp_guard.client import DaemonClient
from mcp_guard.daemon.ui_auto import constant
from mcp_guard.proxy.http import HttpGuardProxy

HEADERS = {"Accept": "application/json, text/event-stream", "User-Agent": "Claude-User"}


def run(coro):
    return asyncio.run(coro)


async def _with_proxy(settings, fn, path_secret=None):
    upstream = TestServer(make_upstream())
    await upstream.start_server()
    proxy = HttpGuardProxy(str(upstream.make_url("")), "demo-remote", settings,
                           DaemonClient(settings), path_secret=path_secret)
    server = TestServer(proxy.make_app())
    await server.start_server()
    try:
        async with ClientSession() as http:
            return await fn(http, server)
    finally:
        await server.close()
        await upstream.close()


def _call(mid, tool, args=None):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}


def test_http_allow_and_block(start_daemon, guard_home):
    d = start_daemon(decider_from(lambda info: ("allow" if info["target"] == "echo" else "block", 0)))

    async def scenario(http, server):
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai"}}}
        r = await http.post(server.make_url("/mcp"), json=init, headers=HEADERS)
        sid = r.headers["Mcp-Session-Id"]
        h = {**HEADERS, "Mcp-Session-Id": sid}
        allowed = await (await http.post(server.make_url("/mcp"), json=_call(1, "echo", {"message": "hi"}), headers=h)).json()
        blocked = await (await http.post(server.make_url("/mcp"), json=_call(2, "delete_all_notes"), headers=h)).json()
        listed = await (await http.post(server.make_url("/mcp"), json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=h)).json()
        return allowed, blocked, listed

    allowed, blocked, listed = run(_with_proxy(d.settings, scenario))
    assert allowed["result"]["content"][0]["text"] == "hi"
    assert blocked["id"] == 2 and blocked["result"]["isError"] is True
    assert len(listed["result"]["tools"]) >= 3
    assert not any("delete_all_notes" in x for x in executions(guard_home))
    assert all(p.info["transport"] == "http" for p in d.presenter.shown)
    assert "remote connector" in d.presenter.shown[0].info["source"]


def test_http_concurrent(start_daemon):
    d = start_daemon(constant("allow", 0.3))

    async def scenario(http, server):
        reqs = [http.post(server.make_url("/mcp"), json=_call(i, "echo", {"message": str(i)}), headers=HEADERS)
                for i in range(8)]
        resps = await asyncio.gather(*reqs)
        return [await r.json() for r in resps]

    out = run(_with_proxy(d.settings, scenario))
    assert sorted(r["result"]["content"][0]["text"] for r in out) == sorted(str(i) for i in range(8))
    assert len(d.presenter.shown) == 8


def test_http_batch_partial_block(start_daemon):
    d = start_daemon(decider_from(lambda info: ("allow" if info["arguments"].get("message") == "yes" else "block", 0)))

    async def scenario(http, server):
        batch = [_call(1, "echo", {"message": "yes"}), _call(2, "echo", {"message": "no"})]
        return await (await http.post(server.make_url("/mcp"), json=batch, headers=HEADERS)).json()

    out = {r["id"]: r for r in run(_with_proxy(d.settings, scenario))}
    assert out[1]["result"]["isError"] is False
    assert out[2]["result"]["isError"] is True


def test_http_path_secret(start_daemon):
    d = start_daemon(constant("allow"))

    async def scenario(http, server):
        bad = await http.post(server.make_url("/mcp"), json=_call(1, "echo", {"message": "x"}), headers=HEADERS)
        good = await http.post(server.make_url("/s3cret/mcp"), json=_call(1, "echo", {"message": "x"}), headers=HEADERS)
        return bad.status, (await good.json())

    status, good = run(_with_proxy(d.settings, scenario, path_secret="s3cret"))
    assert status == 404
    assert good["result"]["content"][0]["text"] == "x"
