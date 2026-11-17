"""JSONL audit log of every decision."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024


class AuditLog:
    def __init__(self, path: Path, include_arguments: bool = True) -> None:
        self.path = path
        self.include_arguments = include_arguments
        self._lock = threading.Lock()

    def write(self, kind: str, **data: Any) -> None:
        if not self.include_arguments and "arguments" in data:
            data["arguments"] = "<redacted>"
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, **data}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                log.error("Audit write failed: %s", exc)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
