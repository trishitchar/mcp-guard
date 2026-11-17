"""Pending requests and their decisions.

Each request waits on its own Event. decide() is the only way to resolve one and
runs under a lock, so a request is answered exactly once (click vs timeout race).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from mcp_guard.audit import AuditLog
from mcp_guard.config import Settings
from mcp_guard.daemon.policy import Policy

log = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    approval_id: str
    info: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    deadline: float = 0.0
    decision: str | None = None
    reason: str = ""
    decided_by: str = ""
    event: threading.Event = field(default_factory=threading.Event)

    @property
    def server(self) -> str:
        return str(self.info.get("server", "?"))

    @property
    def target(self) -> str:
        return str(self.info.get("target", "?"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "info": self.info,
            "created_at": self.created_at,
            "deadline": self.deadline,
        }


class Presenter(Protocol):
    def show(self, pending: PendingApproval) -> None: ...
    def dismiss(self, approval_id: str) -> None: ...
    def activity(self, record: dict[str, Any]) -> None: ...


class ApprovalBroker:
    def __init__(self, settings: Settings, policy: Policy, audit: AuditLog) -> None:
        self.settings = settings
        self.policy = policy
        self.audit = audit
        self.presenter: Presenter | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._remembered: dict[tuple[str, str], str] = {}
        self._closed = False

    def request_decision(self, approval_id: str, info: dict[str, Any]) -> PendingApproval:
        """Blocks the calling thread until the request is decided."""
        pending = PendingApproval(approval_id, info)
        pending.deadline = pending.created_at + self.settings.decision_timeout_s

        auto = self._automatic_decision(pending)
        if auto is not None:
            pending.decision, pending.reason, pending.decided_by = auto
            self._record(pending)
            return pending

        with self._lock:
            if approval_id in self._pending:  # retry: wait on the original
                pending = self._pending[approval_id]
            else:
                self._pending[approval_id] = pending
                if self.presenter:
                    self.presenter.show(pending)

        remaining = max(0.0, pending.deadline - time.time())
        if not pending.event.wait(remaining):
            self.decide(
                approval_id,
                self.settings.timeout_action,
                by="timeout",
                reason=f"no decision within {self.settings.decision_timeout_s:.0f}s",
            )
            pending.event.wait(1)
        return pending

    def _automatic_decision(self, p: PendingApproval) -> tuple[str, str, str] | None:
        if self._closed:
            return "block", "MCP Guard is shutting down", "shutdown"
        action, rule = self.policy.evaluate(p.server, p.target)
        if action in ("allow", "block"):
            return action, f"policy rule '{rule.match}'", "policy"  # type: ignore[union-attr]
        with self._lock:
            remembered = self._remembered.get((p.server, p.target))
        if remembered:
            return remembered, "remembered for this session", "session-rule"
        return None

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        by: str = "user",
        reason: str = "",
        remember: bool = False,
    ) -> bool:
        """Resolve a pending request. Returns False if it was already resolved."""
        if decision not in ("allow", "block"):
            raise ValueError(decision)
        with self._lock:
            pending = self._pending.pop(approval_id, None)
            if pending is None or pending.decision is not None:
                return False
            pending.decision = decision
            pending.decided_by = by
            pending.reason = reason or ("allowed by user" if decision == "allow" else "blocked by user")
            if remember:
                self._remembered[(pending.server, pending.target)] = decision
        # Log before waking the waiter so the audit log is never behind the reply.
        self._record(pending)
        if self.presenter:
            self.presenter.dismiss(approval_id)
        pending.event.set()
        return True

    def cancel(self, approval_id: str, reason: str = "cancelled by client") -> bool:
        return self.decide(approval_id, "block", by="cancelled", reason=reason)

    def pending(self) -> list[PendingApproval]:
        with self._lock:
            return list(self._pending.values())

    def forget_session_rules(self) -> None:
        with self._lock:
            self._remembered.clear()

    def shutdown(self) -> None:
        """Block everything still waiting."""
        self._closed = True
        for p in self.pending():
            self.decide(p.approval_id, "block", by="shutdown", reason="MCP Guard is shutting down")

    def _record(self, p: PendingApproval) -> None:
        record = {
            "approval_id": p.approval_id,
            "decision": p.decision,
            "decided_by": p.decided_by,
            "reason": p.reason,
            "latency_ms": int((time.time() - p.created_at) * 1000),
            **{k: p.info.get(k) for k in ("source", "transport", "server", "method", "target", "arguments")},
        }
        self.audit.write("decision", **record)
        log.info("%s %s:%s -> %s (%s)", p.info.get("method"), p.server, p.target, p.decision, p.decided_by)
        if self.presenter:
            self.presenter.activity({"kind": "decision", **record})

    def event(self, kind: str, data: dict[str, Any]) -> None:
        self.audit.write(kind, **data)
        if self.presenter:
            self.presenter.activity({"kind": kind, **data})
