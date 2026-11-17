from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO_STDIO = ROOT / "examples" / "demo_stdio_server.py"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from mcp_guard.audit import AuditLog  # noqa: E402
from mcp_guard.config import Settings  # noqa: E402
from mcp_guard.daemon.broker import ApprovalBroker  # noqa: E402
from mcp_guard.daemon.policy import Policy  # noqa: E402
from mcp_guard.daemon.server import start_server  # noqa: E402
from mcp_guard.daemon.ui_auto import AutoPresenter, Decider  # noqa: E402


@pytest.fixture
def guard_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "guard_home"
    home.mkdir()
    monkeypatch.setenv("MCP_GUARD_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    return home


@dataclass
class Daemon:
    broker: ApprovalBroker
    presenter: AutoPresenter
    settings: Settings


@pytest.fixture
def start_daemon(guard_home, monkeypatch):
    """Factory: start an in-process daemon with a programmable decider."""
    servers = []

    def _start(decider: Decider, timeout: float = 10.0, rules=None) -> Daemon:
        settings = Settings(port=0, decision_timeout_s=timeout, rules=rules or [])
        broker = ApprovalBroker(settings, Policy(settings.rules), AuditLog(guard_home / "audit.jsonl"))
        presenter = AutoPresenter(broker, decider)
        broker.presenter = presenter
        server = start_server(broker, "127.0.0.1", 0)
        servers.append((broker, server))
        settings.port = server.server_address[1]
        monkeypatch.setenv("MCP_GUARD_PORT", str(settings.port))
        monkeypatch.setenv("MCP_GUARD_TIMEOUT", str(timeout))
        return Daemon(broker, presenter, settings)

    yield _start
    for broker, server in servers:
        broker.shutdown()
        server.shutdown()
        server.server_close()


class McpSession:
    """Drive an MCP server (through the guard proxy) over stdio like Claude does."""

    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=dict(os.environ))
        self.inbox: "queue.Queue[Any]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.stderr: list[str] = []

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self.inbox.put(json.loads(line))

    def _drain_stderr(self) -> None:
        for line in iter(self.proc.stderr.readline, b""):
            self.stderr.append(line.decode(errors="replace"))

    def send(self, msg: Any) -> None:
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 10) -> Any:
        return self.inbox.get(timeout=timeout)

    def request(self, mid: Any, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def initialize(self) -> dict:
        self.request(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "claude-ai", "version": "0.test"}})
        resp = self.recv()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=10)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


@pytest.fixture
def guarded_demo(guard_home):
    sessions: list[McpSession] = []

    def _open(name: str = "demo") -> McpSession:
        cmd = [sys.executable, "-m", "mcp_guard", "stdio", "--name", name, "--", sys.executable, str(DEMO_STDIO)]
        s = McpSession(cmd)
        sessions.append(s)
        return s

    yield _open
    for s in sessions:
        s.close()


def executions(home: Path) -> list[str]:
    log = home / "demo_executions.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def decider_from(fn: Callable[[dict], "tuple[str | None, float]"]) -> Decider:
    return lambda pending: fn(pending.info)
