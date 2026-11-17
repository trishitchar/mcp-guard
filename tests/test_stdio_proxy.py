"""End-to-end: real proxy subprocess + real demo server + in-process daemon."""

from __future__ import annotations

import queue
import time

import pytest

from conftest import decider_from, executions
from mcp_guard.daemon.ui_auto import constant


def test_allowed_call_reaches_server(start_daemon, guarded_demo, guard_home):
    d = start_daemon(constant("allow"))
    s = guarded_demo()
    assert s.initialize()["result"]["serverInfo"]["name"] == "mcp-guard-demo"
    s.request(1, "tools/call", {"name": "echo", "arguments": {"message": "hello"}})
    resp = s.recv()
    assert resp["id"] == 1
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["text"] == "hello"
    assert any("EXECUTED echo" in line for line in executions(guard_home))
    # The popup received the full request details.
    shown = d.presenter.shown[0].info
    assert shown["server"] == "demo" and shown["target"] == "echo"
    assert shown["arguments"] == {"message": "hello"}
    assert shown["source"].startswith("Claude Desktop")


def test_blocked_call_never_reaches_server(start_daemon, guarded_demo, guard_home):
    start_daemon(constant("block"))
    s = guarded_demo()
    s.initialize()
    s.request(7, "tools/call", {"name": "delete_all_notes", "arguments": {}})
    resp = s.recv()
    assert resp["id"] == 7
    assert resp["result"]["isError"] is True
    assert "blocked by MCP Guard" in resp["result"]["content"][0]["text"]
    assert not any("delete_all_notes" in line for line in executions(guard_home))


def test_non_gated_methods_pass_through(start_daemon, guarded_demo):
    d = start_daemon(constant("block"))
    s = guarded_demo()
    s.initialize()
    s.request(2, "tools/list")
    assert {t["name"] for t in s.recv()["result"]["tools"]} >= {"echo", "get_time"}
    s.request(3, "ping")
    assert s.recv()["result"] == {}
    assert d.presenter.shown == []  # nothing needed approval


def test_concurrent_requests_resolved_independently(start_daemon, guarded_demo, guard_home):
    # Allow messages starting with "ok"; later requests are decided *first*.
    def decide(info):
        msg = info["arguments"]["message"]
        n = int(msg.split("-")[1])
        return ("allow" if msg.startswith("ok") else "block"), 0.6 - n * 0.1

    d = start_daemon(decider_from(decide))
    s = guarded_demo()
    s.initialize()
    expected = {}
    for n in range(6):
        msg = f"{'ok' if n % 2 == 0 else 'no'}-{n}"
        expected[100 + n] = msg
        s.request(100 + n, "tools/call", {"name": "echo", "arguments": {"message": msg}})
    s.request(999, "ping")  # must not be stuck behind pending approvals
    first = s.recv()
    assert first["id"] == 999

    got = {}
    for _ in range(6):
        r = s.recv()
        got[r["id"]] = r
    assert set(got) == set(expected)
    order = list(got)
    assert order[0] == 105  # answered in decision order, not request order
    for rid, msg in expected.items():
        res = got[rid]["result"]
        if msg.startswith("ok"):
            assert res["isError"] is False and res["content"][0]["text"] == msg
        else:
            assert res["isError"] is True
    assert len(d.presenter.shown) == 6
    executed = [line for line in executions(guard_home) if "EXECUTED echo" in line]
    assert len(executed) == 3


def test_daemon_down_fails_closed(guarded_demo, guard_home, monkeypatch):
    monkeypatch.setenv("MCP_GUARD_PORT", "1")  # nothing listens here
    s = guarded_demo()
    s.initialize()
    s.request(1, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    resp = s.recv()
    assert resp["result"]["isError"] is True
    assert "not running" in resp["result"]["content"][0]["text"]
    assert executions(guard_home) == []


def test_daemon_down_fail_open(guarded_demo, guard_home, monkeypatch):
    monkeypatch.setenv("MCP_GUARD_PORT", "1")
    monkeypatch.setenv("MCP_GUARD_FAIL_MODE", "open")
    s = guarded_demo()
    s.initialize()
    s.request(1, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    assert s.recv()["result"]["isError"] is False


def test_timeout_blocks(start_daemon, guarded_demo):
    start_daemon(constant(None), timeout=1.0)  # nobody answers
    s = guarded_demo()
    s.initialize()
    t0 = time.time()
    s.request(1, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    resp = s.recv(timeout=10)
    assert resp["result"]["isError"] is True
    assert "no decision within" in resp["result"]["content"][0]["text"]
    assert time.time() - t0 < 6


def test_client_cancellation_closes_popup(start_daemon, guarded_demo):
    d = start_daemon(constant(None))
    s = guarded_demo()
    s.initialize()
    s.request(5, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    for _ in range(50):
        if d.presenter.shown:
            break
        time.sleep(0.05)
    aid = d.presenter.shown[0].approval_id
    s.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
    for _ in range(50):
        if aid in d.presenter.dismissed:
            break
        time.sleep(0.05)
    assert aid in d.presenter.dismissed
    assert d.broker.pending() == []
    with pytest.raises(queue.Empty):  # no response for a cancelled request
        s.recv(timeout=1)


def test_client_disconnect_cancels_pending_and_exits(start_daemon, guarded_demo):
    d = start_daemon(constant(None))
    s = guarded_demo()
    s.initialize()
    s.request(5, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    for _ in range(50):
        if d.presenter.shown:
            break
        time.sleep(0.05)
    assert s.close() == 0  # proxy shuts the server down and exits cleanly
    assert d.broker.pending() == []


def test_jsonrpc_error_style(start_daemon, guarded_demo, monkeypatch, guard_home):
    (guard_home / "config.json").write_text('{"block_response": "jsonrpc_error"}')
    start_daemon(constant("block"))
    s = guarded_demo()
    s.initialize()
    s.request(1, "tools/call", {"name": "echo", "arguments": {"message": "x"}})
    resp = s.recv()
    assert resp["error"]["code"] == -32001
