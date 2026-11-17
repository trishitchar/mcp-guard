"""stdio proxy: Claude Desktop starts this instead of the server, and this starts the server.

    Claude <-stdin/stdout-> StdioProxy <-stdin/stdout-> real server

Gated requests (tools/call) are held while a worker thread asks the daemon,
then forwarded or answered with a denial. All other lines pass through as-is.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from typing import IO, Any

from mcp_guard import jsonrpc
from mcp_guard.client import DaemonClient, DaemonUnavailable, Decision
from mcp_guard.config import Settings
from mcp_guard.proxy import build_info, new_approval_id

log = logging.getLogger("mcp_guard.stdio")


def resolve_command(command: list[str], env: dict[str, str]) -> list[str]:
    """Resolve on PATH (Windows needs this for npx.cmd-style shims)."""
    exe = shutil.which(command[0], path=env.get("PATH")) or command[0]
    return [exe, *command[1:]]


def _describe_client(client_info: dict[str, Any]) -> str:
    name = str(client_info.get("name", "")).strip()
    version = client_info.get("version", "")
    if name in ("claude-ai", "claude-desktop") or "claude" in name.lower():
        return f"Claude Desktop ({name} {version})".strip()
    return f"{name} {version}".strip() or "MCP client (stdio)"


class StdioProxy:
    def __init__(
        self,
        server_name: str,
        command: list[str],
        settings: Settings,
        client: DaemonClient | None = None,
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("no MCP server command given")
        self.name = server_name
        self.settings = settings
        self.client = client or DaemonClient(settings)
        self.env = dict(os.environ if env is None else env)
        self.command = resolve_command(command, self.env)
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer

        self.child: subprocess.Popen[bytes] | None = None
        self.client_info: dict[str, Any] = {}
        self._out_lock = threading.Lock()  # writes to Claude
        self._in_lock = threading.Lock()  # writes to the server
        self._state_lock = threading.Lock()
        self._held: dict[Any, str] = {}  # jsonrpc id -> approval id
        self._done = threading.Event()

    def run(self) -> int:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self.child = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # inherit, so server logs reach Claude's log file
                env=self.env,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            log.error("Failed to start MCP server %r: %s", self.command, exc)
            return 127

        log.info("Guarding MCP server '%s' (pid %s): %s", self.name, self.child.pid, self.command)
        self.client.send_event(
            "server_started",
            {"server": self.name, "transport": "stdio", "command": " ".join(self.command), "pid": self.child.pid},
        )
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self._done.set())
                except (ValueError, OSError):
                    pass

        threading.Thread(target=self._pump_client_to_server, name="client->server", daemon=True).start()
        threading.Thread(target=self._pump_server_to_client, name="server->client", daemon=True).start()

        try:
            self._done.wait()
        except KeyboardInterrupt:
            pass
        return self._shutdown()

    def _shutdown(self) -> int:
        # Close pop-ups that can no longer be answered.
        with self._state_lock:
            held, self._held = dict(self._held), {}
        for approval_id in held.values():
            self.client.cancel(approval_id, "MCP client or server disconnected")

        child = self.child
        code = 0
        if child is not None:
            try:
                if child.stdin:
                    child.stdin.close()
            except OSError:
                pass
            try:
                code = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    code = child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    code = child.wait()
        self.client.send_event("server_stopped", {"server": self.name, "transport": "stdio", "detail": f"exit {code}"})
        log.info("MCP server '%s' exited with %s", self.name, code)
        return code or 0

    def _pump_client_to_server(self) -> None:
        try:
            for line in iter(self.stdin.readline, b""):
                if line.strip():
                    self._on_client_line(line)
        except (OSError, ValueError) as exc:
            log.warning("Reading from client failed: %s", exc)
        finally:
            log.info("Client closed stdin")
            self._done.set()

    def _pump_server_to_client(self) -> None:
        assert self.child and self.child.stdout
        try:
            for line in iter(self.child.stdout.readline, b""):
                self._write_client(line)
        except (OSError, ValueError) as exc:
            log.warning("Reading from server failed: %s", exc)
        finally:
            log.info("Server closed stdout")
            self._done.set()

    def _on_client_line(self, line: bytes) -> None:
        msg = jsonrpc.parse(line)
        if isinstance(msg, dict):
            self._on_client_message(msg, line)
        elif isinstance(msg, list) and any(jsonrpc.is_gated(m, self.settings.gated_methods) for m in msg):
            threading.Thread(target=self._handle_batch, args=(msg,), daemon=True).start()
        else:
            self._write_server(line)

    def _on_client_message(self, msg: dict[str, Any], line: bytes) -> None:
        method = msg.get("method")
        if method == "initialize" and isinstance(msg.get("params"), dict):
            self.client_info = msg["params"].get("clientInfo") or {}
            self.client.send_event(
                "client_connected",
                {"server": self.name, "source": _describe_client(self.client_info), "transport": "stdio",
                 "detail": f"protocol {msg['params'].get('protocolVersion', '?')}"},
            )

        if method == "notifications/cancelled":
            req_id = (msg.get("params") or {}).get("requestId")
            with self._state_lock:
                approval_id = self._held.pop(req_id, None)
            if approval_id:
                # The server never saw the request, so the cancel isn't forwarded.
                log.info("Client cancelled held request %r", req_id)
                self.client.cancel(approval_id, "cancelled by Claude")
                return

        if jsonrpc.is_gated(msg, self.settings.gated_methods):
            approval_id = new_approval_id()
            with self._state_lock:
                self._held[msg["id"]] = approval_id
            threading.Thread(
                target=self._handle_gated, args=(msg, line, approval_id), daemon=True, name=f"approval-{msg['id']}"
            ).start()
            return

        self._write_server(line)

    def _ask(self, msg: dict[str, Any], approval_id: str) -> Decision:
        info = build_info(msg, server=self.name, source=_describe_client(self.client_info),
                          transport="stdio", client_info=self.client_info)
        try:
            return self.client.request_approval(approval_id, info)
        except DaemonUnavailable as exc:
            log.error("Approval daemon unavailable: %s", exc)
            if self.settings.fail_mode == "open":
                return Decision("allow", "daemon unavailable (fail-open)", "fail-open")
            return Decision(
                "block",
                "the MCP Guard approval app is not running, so the request could not be confirmed. "
                "Start it with `mcp-guard daemon`.",
                "fail-closed",
            )

    def _handle_gated(self, msg: dict[str, Any], line: bytes, approval_id: str) -> None:
        decision = self._ask(msg, approval_id)
        with self._state_lock:
            still_held = self._held.pop(msg["id"], None) == approval_id
        if not still_held:
            return  # cancelled meanwhile
        if decision.allowed:
            if not self._write_server(line):
                self._write_client(jsonrpc.dumps(jsonrpc.error_response(
                    msg["id"], -32603, "MCP server is not running")) + b"\n")
        else:
            denial = jsonrpc.denial_response(msg, decision.reason, self.settings.block_response)
            self._write_client(jsonrpc.dumps(denial) + b"\n")

    def _handle_batch(self, batch: list[Any]) -> None:
        """Approve gated items concurrently; forward the rest as one batch."""
        results: dict[int, Decision] = {}

        def worker(i: int, m: dict[str, Any]) -> None:
            results[i] = self._ask(m, new_approval_id())

        threads = [threading.Thread(target=worker, args=(i, m), daemon=True)
                   for i, m in enumerate(batch) if jsonrpc.is_gated(m, self.settings.gated_methods)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        forward = [m for i, m in enumerate(batch) if i not in results or results[i].allowed]
        denials = [jsonrpc.denial_response(batch[i], d.reason, self.settings.block_response)
                   for i, d in results.items() if not d.allowed]
        if forward:
            self._write_server(jsonrpc.dumps(forward) + b"\n")
        if denials:
            self._write_client(jsonrpc.dumps(denials) + b"\n")

    def _write_server(self, data: bytes) -> bool:
        if not data.endswith(b"\n"):
            data += b"\n"
        with self._in_lock:
            try:
                assert self.child and self.child.stdin
                self.child.stdin.write(data)
                self.child.stdin.flush()
                return True
            except (OSError, ValueError, AssertionError) as exc:
                log.error("Write to server failed: %s", exc)
                return False

    def _write_client(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except (OSError, ValueError) as exc:
                log.error("Write to client failed: %s", exc)
                self._done.set()


def run_stdio_proxy(server_name: str, command: list[str], settings: Settings) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format=f"[mcp-guard:{server_name}] %(levelname)s %(message)s",
    )
    return StdioProxy(server_name, command, settings).run()
