from __future__ import annotations

import os
from dataclasses import dataclass, field
from time import monotonic
from typing import Any


DEFAULT_INTERVALS = {
    "send_order": 0.35,
    "cancel_order": 0.35,
    "modify_order": 0.35,
    "tr_request": 0.8,
    "send_condition": 0.5,
    "register_realtime": 0.5,
    "remove_realtime": 0.5,
    "login": 1.0,
    "load_conditions": 1.0,
    "*": 0.2,
}


@dataclass
class RateLimiter:
    min_intervals: dict[str, float] = field(default_factory=dict)
    _last_at: dict[str, float] = field(default_factory=dict)
    _allowed_count: dict[str, int] = field(default_factory=dict)
    _limited_count: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "GATEWAY_RATE_LIMIT_") -> "RateLimiter":
        intervals = dict(DEFAULT_INTERVALS)
        for command_type in list(intervals):
            env_name = f"{prefix}{_env_key(command_type)}_SEC"
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            try:
                intervals[command_type] = max(0.0, float(raw))
            except ValueError:
                continue
        raw_default = os.environ.get(f"{prefix}DEFAULT_SEC")
        if raw_default is not None:
            try:
                intervals["*"] = max(0.0, float(raw_default))
            except ValueError:
                pass
        return cls(intervals)

    def allow(self, command_type: str, now: float | None = None) -> bool:
        wait = self.wait_time(command_type, now=now)
        if wait > 0:
            self._limited_count[command_type] = self._limited_count.get(command_type, 0) + 1
            return False
        return True

    def wait_time(self, command_type: str, now: float | None = None) -> float:
        current = monotonic() if now is None else float(now)
        interval = self.min_intervals.get(command_type, self.min_intervals.get("*", DEFAULT_INTERVALS["*"]))
        last_at = self._last_at.get(command_type)
        if last_at is None:
            return 0.0
        return max(0.0, (last_at + max(0.0, interval)) - current)

    def record(self, command_type: str, now: float | None = None) -> None:
        current = monotonic() if now is None else float(now)
        self._last_at[command_type] = current
        self._allowed_count[command_type] = self._allowed_count.get(command_type, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        command_types = sorted(set(self.min_intervals) | set(self._last_at) | set(self._allowed_count) | set(self._limited_count))
        return {
            "policy": dict(self.min_intervals),
            "commands": {
                command_type: {
                    "min_interval_sec": self.min_intervals.get(command_type, self.min_intervals.get("*", DEFAULT_INTERVALS["*"])),
                    "allowed_count": self._allowed_count.get(command_type, 0),
                    "limited_count": self._limited_count.get(command_type, 0),
                    "wait_time_sec": self.wait_time(command_type),
                }
                for command_type in command_types
            },
        }


def _env_key(command_type: str) -> str:
    if command_type == "*":
        return "DEFAULT"
    return command_type.upper()
