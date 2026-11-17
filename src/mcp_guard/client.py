"""Proxy -> daemon client. Approval is a long poll that returns once the user decides."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from mcp_guard import paths
from mcp_guard.config import Settings

log = logging.getLogger(__name__)


class DaemonUnavailable(Exception):
    pass


@dataclass
class Decision:
    decision: str  # "allow" | "block"
    reason: str = ""
    decided_by: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class DaemonClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _token(self) -> str:
        try:
            return paths.token_path().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DaemonUnavailable("token file missing - is the daemon running?") from exc

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            self.settings.daemon_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise DaemonUnavailable(f"daemon returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DaemonUnavailable(str(exc)) from exc

    def health(self, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(self.settings.daemon_url + "/v1/health", timeout=timeout) as r:
                return json.loads(r.read()).get("app") == "mcp-guard"
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def request_approval(self, approval_id: str, info: dict[str, Any]) -> Decision:
        timeout = self.settings.decision_timeout_s + 15
        data = self._post("/v1/approvals", {"approval_id": approval_id, "info": info}, timeout)
        decision = data.get("decision")
        if decision not in ("allow", "block"):
            raise DaemonUnavailable(f"malformed decision: {data!r}")
        return Decision(decision, data.get("reason", ""), data.get("decided_by", ""))

    def cancel(self, approval_id: str, reason: str = "cancelled by client") -> None:
        try:
            self._post(f"/v1/approvals/{approval_id}/cancel", {"reason": reason}, timeout=3)
        except DaemonUnavailable:
            pass

    def send_event(self, kind: str, data: dict[str, Any]) -> None:
        """Fire-and-forget activity event."""

        def _send() -> None:
            try:
                self._post("/v1/events", {"kind": kind, "data": data}, timeout=3)
            except DaemonUnavailable:
                pass

        threading.Thread(target=_send, daemon=True, name="mcp-guard-event").start()

    def show_window(self) -> bool:
        try:
            self._post("/v1/show", {}, timeout=3)
            return True
        except DaemonUnavailable:
            return False
