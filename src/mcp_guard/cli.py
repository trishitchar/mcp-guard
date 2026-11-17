"""Command line entry point: ``mcp-guard <command>`` / ``python -m mcp_guard``."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from mcp_guard import __version__


def _cmd_daemon(a: argparse.Namespace) -> int:
    from mcp_guard.daemon.app import run_daemon

    return run_daemon(headless=a.headless, minimized=a.minimized)


def _cmd_stdio(a: argparse.Namespace) -> int:
    from mcp_guard.config import load_settings
    from mcp_guard.proxy.stdio import run_stdio_proxy

    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        print("usage: mcp-guard stdio --name NAME -- <server command> [args...]", file=sys.stderr)
        return 2
    return run_stdio_proxy(a.name, command, load_settings())


def _cmd_http(a: argparse.Namespace) -> int:
    from mcp_guard.config import load_settings
    from mcp_guard.proxy.http import run_http_proxy

    return run_http_proxy(a.upstream, a.name, load_settings(), a.host, a.port, a.path_secret)


def _print_report(action: str, r) -> None:
    print(f"Config: {r.config_path}")
    for name in r.changed:
        print(f"  {action}: {name}")
    for name, why in r.skipped.items():
        print(f"  skipped: {name} ({why})")
    if not r.changed:
        print("  nothing changed")
    if r.backup:
        print(f"Backup written to {r.backup}")
    if r.changed:
        print("Fully quit Claude Desktop (tray icon -> Quit) and start it again to apply.")


def _cmd_install(a: argparse.Namespace) -> int:
    from mcp_guard.installer import claude_desktop

    r = claude_desktop.install(Path(a.config) if a.config else None, a.server or None,
                               add_demo=a.add_demo, dry_run=a.dry_run)
    _print_report("guarded" + (" (dry run)" if a.dry_run else ""), r)
    return 0


def _cmd_uninstall(a: argparse.Namespace) -> int:
    from mcp_guard.installer import claude_desktop

    r = claude_desktop.uninstall(Path(a.config) if a.config else None, dry_run=a.dry_run)
    _print_report("restored" + (" (dry run)" if a.dry_run else ""), r)
    return 0


def _cmd_status(a: argparse.Namespace) -> int:
    from mcp_guard.client import DaemonClient
    from mcp_guard.config import load_settings
    from mcp_guard.installer import claude_desktop

    settings = load_settings()
    up = DaemonClient(settings).health()
    print(f"Daemon : {'RUNNING' if up else 'NOT RUNNING'} at {settings.daemon_url}")
    print(f"Mode   : fail-{settings.fail_mode}, timeout {settings.decision_timeout_s:.0f}s -> "
          f"{settings.timeout_action}, gated {settings.gated_methods}")
    path, servers = claude_desktop.status(Path(a.config) if a.config else None)
    print(f"Claude Desktop config: {path}{'' if path.exists() else ' (not found)'}")
    for name, guarded in servers.items():
        print(f"  [{'guarded' if guarded else 'NOT guarded'}] {name}")
    return 0 if up else 1


def _cmd_test_popup(a: argparse.Namespace) -> int:
    """Send fake approval requests to the daemon to check the pop-up."""
    from mcp_guard.client import DaemonClient, DaemonUnavailable
    from mcp_guard.config import load_settings
    from mcp_guard.proxy import new_approval_id

    client = DaemonClient(load_settings())
    results: list[str] = []

    def one(i: int) -> None:
        info = {"server": "test-server", "method": "tools/call", "target": f"example_tool_{i}",
                "arguments": {"path": f"C:/Users/example/file{i}.txt", "mode": "write"},
                "source": "mcp-guard test-popup", "transport": "test"}
        try:
            d = client.request_approval(new_approval_id(), info)
            results.append(f"request {i}: {d.decision.upper()} ({d.decided_by}: {d.reason})")
        except DaemonUnavailable as exc:
            results.append(f"request {i}: daemon unavailable: {exc}")

    threads = [threading.Thread(target=one, args=(i,)) for i in range(1, a.count + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n".join(sorted(results)))
    return 0


def _cmd_config(a: argparse.Namespace) -> int:
    from dataclasses import asdict

    from mcp_guard import paths
    from mcp_guard.config import load_settings

    print(f"# {paths.config_path()}")
    print(json.dumps(asdict(load_settings()), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block every MCP request from Claude.")
    p.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the background approval app (pop-ups)")
    d.add_argument("--minimized", action="store_true", help="start with the activity window hidden")
    d.add_argument("--headless", choices=["allow", "block"], help="no UI; auto-decide (testing only)")
    d.set_defaults(func=_cmd_daemon)

    s = sub.add_parser("stdio", help="wrap a local stdio MCP server (used by Claude Desktop)")
    s.add_argument("--name", required=True, help="server name shown in the pop-up")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    s.set_defaults(func=_cmd_stdio)

    h = sub.add_parser("http", help="guard a remote Streamable-HTTP MCP server (Claude Web connectors)")
    h.add_argument("--upstream", required=True, help="real MCP server URL, e.g. http://127.0.0.1:9000")
    h.add_argument("--name", default="remote", help="server name shown in the pop-up")
    h.add_argument("--host", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8787)
    h.add_argument("--path-secret", default=None,
                   help="require /<secret>/ URL prefix ('auto' generates one) - recommended when tunnelled")
    h.set_defaults(func=_cmd_http)

    for name, fn, helptext in (("install", _cmd_install, "guard servers in claude_desktop_config.json"),
                               ("uninstall", _cmd_uninstall, "restore original claude_desktop_config.json entries")):
        i = sub.add_parser(name, help=helptext)
        i.add_argument("--config", help="path to claude_desktop_config.json (auto-detected by default)")
        i.add_argument("--dry-run", action="store_true")
        if name == "install":
            i.add_argument("--server", action="append", help="only guard this server (repeatable)")
            i.add_argument("--add-demo", action="store_true", help="also add the bundled demo MCP server")
        i.set_defaults(func=fn)

    st = sub.add_parser("status", help="show daemon status and which servers are guarded")
    st.add_argument("--config")
    st.set_defaults(func=_cmd_status)

    t = sub.add_parser("test-popup", help="send fake requests to the daemon to try the pop-up")
    t.add_argument("--count", type=int, default=1, help="number of simultaneous requests")
    t.set_defaults(func=_cmd_test_popup)

    c = sub.add_parser("config", help="print effective settings")
    c.set_defaults(func=_cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
