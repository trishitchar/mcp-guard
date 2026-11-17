"""Scripted decisions instead of a human (tests and `daemon --headless`)."""

from __future__ import annotations

import threading
from typing import Any, Callable

from mcp_guard.daemon.broker import ApprovalBroker, PendingApproval

# (decision, delay_seconds); decision None = never answer
Decider = Callable[[PendingApproval], "tuple[str | None, float]"]


class AutoPresenter:
    def __init__(self, broker: ApprovalBroker, decider: Decider) -> None:
        self.broker = broker
        self.decider = decider
        self.shown: list[PendingApproval] = []
        self.dismissed: list[str] = []
        self.activity_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def show(self, pending: PendingApproval) -> None:
        with self._lock:
            self.shown.append(pending)
        decision, delay = self.decider(pending)
        if decision is None:
            return
        timer = threading.Timer(delay, self.broker.decide, args=(pending.approval_id, decision))
        timer.daemon = True
        timer.start()

    def dismiss(self, approval_id: str) -> None:
        with self._lock:
            self.dismissed.append(approval_id)

    def activity(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.activity_log.append(record)


def constant(decision: str, delay: float = 0.0) -> Decider:
    return lambda _p: (decision, delay)
