"""Wrap stdio servers in claude_desktop_config.json with the guard proxy.

    {"command": "npx", "args": ["pkg"]}
 -> {"command": "<python>", "args": ["-m", "mcp_guard", "stdio", "--name", "x", "--", "npx", "pkg"]}

The original command stays after "--" so uninstall can restore it. A backup is
written before every change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRAP_MARKER = ["-m", "mcp_guard", "stdio"]


def candidate_config_paths() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        # Newer builds: %LOCALAPPDATA%\Claude-Data\<id>\ (most recent first).
        data_dirs = sorted((local / "Claude-Data").glob("claude_*"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        # Older Microsoft Store builds.
        msix = sorted((local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        return ([p / "claude_desktop_config.json" for p in data_dirs]
                + [p / "claude_desktop_config.json" for p in msix]
                + [appdata / "Claude/claude_desktop_config.json"])
    if sys.platform == "darwin":
        return [home / "Library/Application Support/Claude/claude_desktop_config.json"]
    return [Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "Claude/claude_desktop_config.json"]


def find_config_path() -> Path:
    candidates = candidate_config_paths()
    for path in candidates:
        if path.exists():
            return path
    for path in candidates:
        if path.parent.exists():
            return path
    return candidates[-1]


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return any(args[i:i + 3] == WRAP_MARKER for i in range(len(args)))


def wrap_entry(name: str, entry: dict[str, Any], python: str) -> dict[str, Any]:
    wrapped = dict(entry)
    wrapped["command"] = python
    wrapped["args"] = [*WRAP_MARKER, "--name", name, "--", entry["command"], *(entry.get("args") or [])]
    return wrapped


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    args = list(entry.get("args") or [])
    if "--" not in args:
        raise ValueError("wrapped entry has no '--' separator")
    original = args[args.index("--") + 1:]
    if not original:
        raise ValueError("wrapped entry has no original command")
    restored = dict(entry)
    restored["command"] = original[0]
    restored["args"] = original[1:]
    return restored


@dataclass
class Report:
    config_path: Path
    changed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    backup: Path | None = None


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _save(path: Path, data: dict[str, Any], report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.stem}.mcp-guard-backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, backup)
        report.backup = backup
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic


def install(config_path: Path | None = None, only: list[str] | None = None, python: str | None = None,
            add_demo: bool = False, dry_run: bool = False) -> Report:
    path = config_path or find_config_path()
    report = Report(path)
    data = _load(path)
    servers: dict[str, Any] = data.setdefault("mcpServers", {})
    python = python or sys.executable

    if add_demo and "mcp-guard-demo" not in servers:
        demo = Path(__file__).resolve().parents[3] / "examples" / "demo_stdio_server.py"
        if not demo.exists():
            raise FileNotFoundError(f"demo server not found at {demo} (install with `pip install -e .`)")
        servers["mcp-guard-demo"] = {"command": python, "args": [str(demo)]}

    for name, entry in servers.items():
        if only and name not in only:
            continue
        if not isinstance(entry, dict):
            report.skipped[name] = "invalid entry"
        elif "command" not in entry:
            report.skipped[name] = "not a stdio server (remote/URL) - use `mcp-guard http`"
        elif is_wrapped(entry):
            report.skipped[name] = "already guarded"
        else:
            servers[name] = wrap_entry(name, entry, python)
            report.changed.append(name)

    if report.changed and not dry_run:
        _save(path, data, report)
    return report


def uninstall(config_path: Path | None = None, dry_run: bool = False) -> Report:
    path = config_path or find_config_path()
    report = Report(path)
    data = _load(path)
    servers = data.get("mcpServers") or {}
    for name, entry in list(servers.items()):
        if isinstance(entry, dict) and is_wrapped(entry):
            try:
                servers[name] = unwrap_entry(entry)
                report.changed.append(name)
            except ValueError as exc:
                report.skipped[name] = str(exc)
    if report.changed and not dry_run:
        _save(path, data, report)
    return report


def status(config_path: Path | None = None) -> tuple[Path, dict[str, bool]]:
    path = config_path or find_config_path()
    servers = (_load(path).get("mcpServers") or {})
    return path, {n: isinstance(e, dict) and is_wrapped(e) for n, e in servers.items()}
