"""Settings from ~/.mcp_guard/config.json (all keys optional; see README)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any

from mcp_guard import paths

log = logging.getLogger(__name__)

DEFAULT_PORT = 47821


@dataclass
class Settings:
    host: str = "127.0.0.1"  # loopback only
    port: int = DEFAULT_PORT
    decision_timeout_s: float = 55.0  # Claude gives up after ~60 s
    timeout_action: str = "block"  # "block" | "allow"
    fail_mode: str = "closed"  # daemon down: "closed" blocks, "open" allows
    gated_methods: list[str] = field(default_factory=lambda: ["tools/call"])
    block_response: str = "tool_error"  # "tool_error" | "jsonrpc_error"
    rules: list[dict[str, str]] = field(default_factory=list)  # {"match": "server:tool glob", "action": ...}
    audit_arguments: bool = True
    max_preview_chars: int = 20_000

    @property
    def daemon_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_settings(path: os.PathLike[str] | str | None = None) -> Settings:
    cfg_path = paths.config_path() if path is None else path
    data: dict[str, Any] = {}
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root must be a JSON object")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("Ignoring invalid config %s: %s", cfg_path, exc)
        data = {}

    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        log.warning("Unknown config keys ignored: %s", ", ".join(sorted(unknown)))
    settings = Settings(**{k: v for k, v in data.items() if k in known})

    if os.environ.get("MCP_GUARD_PORT"):
        settings.port = int(os.environ["MCP_GUARD_PORT"])
    if os.environ.get("MCP_GUARD_TIMEOUT"):
        settings.decision_timeout_s = float(os.environ["MCP_GUARD_TIMEOUT"])
    if os.environ.get("MCP_GUARD_FAIL_MODE"):
        settings.fail_mode = os.environ["MCP_GUARD_FAIL_MODE"]

    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    if s.timeout_action not in ("block", "allow"):
        log.warning("timeout_action=%r invalid, using 'block'", s.timeout_action)
        s.timeout_action = "block"
    if s.fail_mode not in ("closed", "open"):
        log.warning("fail_mode=%r invalid, using 'closed'", s.fail_mode)
        s.fail_mode = "closed"
    if s.block_response not in ("tool_error", "jsonrpc_error"):
        s.block_response = "tool_error"
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Daemon host %r is not loopback; forcing 127.0.0.1", s.host)
        s.host = "127.0.0.1"
    if s.decision_timeout_s <= 0:
        s.decision_timeout_s = 55.0
