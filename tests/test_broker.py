from __future__ import annotations

import threading

from mcp_guard.audit import AuditLog
from mcp_guard.config import Settings
from mcp_guard.daemon.broker import ApprovalBroker
from mcp_guard.daemon.policy import Policy
from mcp_guard.daemon.ui_auto import AutoPresenter, constant


def make(tmp_path, decider=constant("allow"), rules=None, timeout=5.0):
    settings = Settings(decision_timeout_s=timeout, rules=rules or [])
    broker = ApprovalBroker(settings, Policy(settings.rules), AuditLog(tmp_path / "a.jsonl"))
    broker.presenter = AutoPresenter(broker, decider)
    return broker


INFO = {"server": "fs", "target": "write_file", "method": "tools/call", "arguments": {}}


def test_policy_rules_first_match_wins(tmp_path):
    b = make(tmp_path, constant("block"), rules=[
        {"match": "fs:write_*", "action": "allow"},
        {"match": "fs:*", "action": "block"},
    ])
    assert b.request_decision("1", INFO).decision == "allow"
    assert b.request_decision("2", {**INFO, "target": "read"}).decision == "block"
    assert b.presenter.shown == []  # no pop-ups needed


def test_remember_for_session(tmp_path):
    b = make(tmp_path, constant(None))
    t = threading.Thread(target=lambda: b.request_decision("a", INFO))
    t.start()
    while not b.pending():
        pass
    b.decide("a", "allow", remember=True)
    t.join()
    p = b.request_decision("b", INFO)
    assert p.decision == "allow" and p.decided_by == "session-rule"
    b.forget_session_rules()
    b.presenter.decider = constant("block")
    assert b.request_decision("c", INFO).decided_by == "user"  # asked again after forgetting


def test_decide_only_once(tmp_path):
    b = make(tmp_path, constant(None))
    t = threading.Thread(target=lambda: b.request_decision("x", INFO))
    t.start()
    while not b.pending():
        pass
    assert b.decide("x", "block") is True
    assert b.decide("x", "allow") is False  # late click after timeout/other decision
    t.join()


def test_shutdown_blocks_everything_pending(tmp_path):
    b = make(tmp_path, constant(None), timeout=30)
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(b.request_decision(str(i), INFO)))
               for i in range(5)]
    for t in threads:
        t.start()
    while len(b.pending()) < 5:
        pass
    b.shutdown()
    for t in threads:
        t.join(5)
    assert [p.decision for p in results] == ["block"] * 5
    assert b.request_decision("late", INFO).decided_by == "shutdown"


def test_audit_log_written(tmp_path):
    b = make(tmp_path)
    b.request_decision("1", INFO)
    text = (tmp_path / "a.jsonl").read_text()
    assert '"decision": "allow"' in text and "write_file" in text
