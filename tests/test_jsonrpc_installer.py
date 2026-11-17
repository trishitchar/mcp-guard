from __future__ import annotations

import json

from mcp_guard import jsonrpc
from mcp_guard.cli import build_parser
from mcp_guard.installer import claude_desktop as cd


def test_request_info_extraction():
    info = jsonrpc.RequestInfo.from_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "t", "arguments": {"a": 1}}})
    assert (info.method, info.target, info.arguments, info.request_id) == ("tools/call", "t", {"a": 1}, 3)
    r = jsonrpc.RequestInfo.from_message({"id": 1, "method": "resources/read", "params": {"uri": "file:///x"}})
    assert r.target == "file:///x"


def test_gating_ignores_notifications():
    assert jsonrpc.is_gated({"id": 1, "method": "tools/call"}, ["tools/call"])
    assert not jsonrpc.is_gated({"method": "tools/call"}, ["tools/call"])
    assert not jsonrpc.is_gated({"id": 1, "method": "tools/list"}, ["tools/call"])


def test_denial_styles():
    msg = {"id": "abc", "method": "tools/call"}
    tool = jsonrpc.denial_response(msg, "nope")
    assert tool["id"] == "abc" and tool["result"]["isError"] is True
    err = jsonrpc.denial_response(msg, "nope", "jsonrpc_error")
    assert err["error"]["code"] == jsonrpc.ERROR_BLOCKED
    other = jsonrpc.denial_response({"id": 1, "method": "resources/read"}, "nope")
    assert "error" in other


def test_dumps_has_no_newlines():
    assert b"\n" not in jsonrpc.dumps({"text": "a\nb"})


def test_cli_stdio_remainder():
    a = build_parser().parse_args(["stdio", "--name", "fs", "--", "npx", "-y", "pkg", "--flag"])
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    assert a.name == "fs" and cmd == ["npx", "-y", "pkg", "--flag"]


def test_install_uninstall_roundtrip(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/x"],
                   "env": {"A": "1"}},
            "remote": {"url": "https://example.com/mcp"},
        },
    }
    cfg.write_text(json.dumps(original))

    r = cd.install(cfg, python="PY")
    assert r.changed == ["fs"] and "remote" in r.skipped and r.backup.exists()
    data = json.loads(cfg.read_text())
    fs = data["mcpServers"]["fs"]
    assert fs["command"] == "PY"
    assert fs["args"][:6] == ["-m", "mcp_guard", "stdio", "--name", "fs", "--"]
    assert fs["env"] == {"A": "1"} and data["globalShortcut"] == "Ctrl+Space"

    assert cd.install(cfg, python="PY").changed == []  # idempotent
    assert cd.status(cfg)[1] == {"fs": True, "remote": False}

    cd.uninstall(cfg)
    assert json.loads(cfg.read_text()) == original


def test_install_creates_missing_config_with_demo(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    r = cd.install(cfg, python="PY", add_demo=True)
    assert r.changed == ["mcp-guard-demo"]
    entry = json.loads(cfg.read_text())["mcpServers"]["mcp-guard-demo"]
    assert entry["args"][-1].endswith("demo_stdio_server.py")
