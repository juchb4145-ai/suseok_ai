from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from trading.strategy.candidates import normalize_code


class HoldingProvider(Protocol):
    def list_holding_codes(self) -> set[str]:
        ...


@dataclass
class StaticHoldingProvider:
    codes: set[str] = field(default_factory=set)

    def list_holding_codes(self) -> set[str]:
        return {normalize_code(code) for code in self.codes if normalize_code(code)}
