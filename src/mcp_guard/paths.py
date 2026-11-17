"""File locations. Everything lives in ~/.mcp_guard (override with MCP_GUARD_HOME)."""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    override = os.environ.get("MCP_GUARD_HOME")
    path = Path(override) if override else Path.home() / ".mcp_guard"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path() -> Path:
    return home_dir() / "token"


def config_path() -> Path:
    return home_dir() / "config.json"


def audit_path() -> Path:
    return home_dir() / "audit.jsonl"


def log_dir() -> Path:
    path = home_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
