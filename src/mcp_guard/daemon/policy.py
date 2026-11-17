"""Config rules checked before asking. First glob match on "server:tool" wins; default "ask"."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

ACTIONS = ("allow", "block", "ask")


@dataclass(frozen=True)
class Rule:
    match: str
    action: str


class Policy:
    def __init__(self, rules: list[dict[str, str]] | None = None) -> None:
        self.rules: list[Rule] = []
        for raw in rules or []:
            match, action = raw.get("match"), raw.get("action")
            if not match or action not in ACTIONS:
                log.warning("Skipping invalid rule %r", raw)
                continue
            self.rules.append(Rule(match, action))

    def evaluate(self, server: str, target: str) -> tuple[str, Rule | None]:
        key = f"{server}:{target}"
        for rule in self.rules:
            if fnmatch.fnmatchcase(key, rule.match):
                return rule.action, rule
        return "ask", None
