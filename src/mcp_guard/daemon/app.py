"""``mcp-guard daemon`` - start the approval service."""

from __future__ import annotations

import logging
import signal
import threading
from logging.handlers import RotatingFileHandler

from mcp_guard import paths
from mcp_guard.audit import AuditLog
from mcp_guard.client import DaemonClient
from mcp_guard.config import load_settings
from mcp_guard.daemon.broker import ApprovalBroker
from mcp_guard.daemon.policy import Policy
from mcp_guard.daemon.server import start_server

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    handler = RotatingFileHandler(paths.log_dir() / "daemon.log", maxBytes=2_000_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run_daemon(headless: str | None = None, minimized: bool = False) -> int:
    _setup_logging()
    settings = load_settings()

    # Single instance: if already running, show its window and exit.
    client = DaemonClient(settings)
    if client.health():
        log.info("MCP Guard is already running on %s", settings.daemon_url)
        print(f"MCP Guard is already running on {settings.daemon_url}")
        client.show_window()
        return 0

    broker = ApprovalBroker(settings, Policy(settings.rules), AuditLog(paths.audit_path(), settings.audit_arguments))

    if headless:
        from mcp_guard.daemon.ui_auto import AutoPresenter, constant

        broker.presenter = AutoPresenter(broker, constant(headless))
        stop = threading.Event()
        try:
            server = start_server(broker, settings.host, settings.port)
        except OSError as exc:
            print(f"Cannot bind {settings.daemon_url}: {exc}")
            return 1
        print(f"MCP Guard (headless, auto-{headless}) on {settings.daemon_url}. Ctrl+C to stop.")
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(0.5):
            pass
        broker.shutdown()
        server.shutdown()
        return 0

    from mcp_guard.daemon.ui_tk import TkPresenter

    presenter = TkPresenter(broker, settings, start_hidden=minimized, on_quit=broker.shutdown)
    broker.presenter = presenter
    try:
        server = start_server(broker, settings.host, settings.port, on_show=presenter.bring_to_front)
    except OSError as exc:
        log.error("Cannot bind %s: %s", settings.daemon_url, exc)
        print(f"Cannot bind {settings.daemon_url}: {exc}")
        return 1

    signal.signal(signal.SIGINT, lambda *_: presenter.request_quit())
    broker.event("daemon_started", {"detail": settings.daemon_url})
    print(f"MCP Guard running on {settings.daemon_url} (close the window to keep it in the background)")
    try:
        presenter.run()
    finally:
        broker.shutdown()
        server.shutdown()
        log.info("MCP Guard stopped")
    return 0
