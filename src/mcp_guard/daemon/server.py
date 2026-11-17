"""Loopback HTTP API for the proxies (bearer token required except /v1/health).

GET  /v1/health                  liveness
POST /v1/approvals               submit {approval_id, info}, wait for the decision
POST /v1/approvals/<id>/cancel   client gave up
GET  /v1/approvals               list pending
POST /v1/events                  activity events
POST /v1/show                    show the activity window

There is deliberately no "approve" endpoint: only the UI can allow a request.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from mcp_guard import __version__, paths
from mcp_guard.daemon.broker import ApprovalBroker

log = logging.getLogger(__name__)

MAX_BODY = 5 * 1024 * 1024
_CANCEL_RE = re.compile(r"^/v1/approvals/([A-Za-z0-9_\-]{1,100})/cancel$")


def ensure_token() -> str:
    path = paths.token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


class GuardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a second daemon must fail to bind

    def __init__(self, addr, broker: ApprovalBroker, token: str, on_show: Callable[[], None]):
        super().__init__(addr, _Handler)
        self.broker = broker
        self.token = token
        self.on_show = on_show


class _Handler(BaseHTTPRequestHandler):
    server: GuardHTTPServer
    server_version = f"mcp-guard/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # proxy went away

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(supplied.encode(), self.server.token.encode())

    def _json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/health":
            return self._send(200, {"ok": True, "app": "mcp-guard", "version": __version__})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/v1/approvals":
            return self._send(200, {"pending": [p.as_dict() for p in self.server.broker.pending()]})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        body = self._json_body()
        if body is None:
            return self._send(400, {"error": "invalid JSON body"})
        broker = self.server.broker

        if self.path == "/v1/approvals":
            approval_id = str(body.get("approval_id") or secrets.token_hex(8))
            info = body.get("info")
            if not isinstance(info, dict):
                return self._send(400, {"error": "info must be an object"})
            pending = broker.request_decision(approval_id, info)  # blocks
            return self._send(
                200,
                {
                    "approval_id": approval_id,
                    "decision": pending.decision or "block",
                    "reason": pending.reason,
                    "decided_by": pending.decided_by,
                },
            )

        m = _CANCEL_RE.match(self.path)
        if m:
            ok = broker.cancel(m.group(1), str(body.get("reason", "cancelled by client")))
            return self._send(200, {"cancelled": ok})

        if self.path == "/v1/events":
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            broker.event(str(body.get("kind", "event"))[:64], data)
            return self._send(200, {"ok": True})

        if self.path == "/v1/show":
            self.server.on_show()
            return self._send(200, {"ok": True})

        self._send(404, {"error": "not found"})


def start_server(
    broker: ApprovalBroker, host: str, port: int, on_show: Callable[[], None] = lambda: None
) -> GuardHTTPServer:
    """Serve in a background thread. Raises OSError if the port is taken."""
    server = GuardHTTPServer((host, port), broker, ensure_token(), on_show)
    threading.Thread(target=server.serve_forever, name="mcp-guard-http", daemon=True).start()
    log.info("Approval API listening on http://%s:%d", host, server.server_address[1])
    return server
