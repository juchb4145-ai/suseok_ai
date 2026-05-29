from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LegStatus(str, Enum):
    WAITING = "대기"
    WATCHING = "접근감시"
    ORDER_SENT = "주문전송"
    UNFILLED = "미체결"
    PARTIALLY_FILLED = "일부체결"
    FILLED = "완료"


@dataclass
class BuyLeg:
    index: int
    target_price: int = 0
    weight_percent: float = 0.0
    status: LegStatus = LegStatus.WAITING
    order_no: str = ""
    ordered_quantity: int = 0
    filled_quantity: int = 0


@dataclass
class WatchItem:
    code: str
    name: str = ""
    budget: int = 0
    stop_loss_price: int = 0
    tick_threshold: int = 1
    take_profit_rate: float = 5.0
    take_profit_sell_percent: float = 70.0
    auto_buy_enabled: bool = False
    auto_sell_enabled: bool = True
    take_profit_done: bool = False
    current_price: int = 0
    average_price: float = 0.0
    holding_quantity: int = 0
    legs: list[BuyLeg] = field(default_factory=lambda: [BuyLeg(1), BuyLeg(2), BuyLeg(3)])

    def leg(self, index: int) -> BuyLeg:
        for leg in self.legs:
            if leg.index == index:
                return leg
        raise KeyError(index)


@dataclass(frozen=True)
class PriceTick:
    code: str
    price: int
    change_rate: float = 0.0
    volume: int = 0
    best_ask: int = 0
    best_bid: int = 0
