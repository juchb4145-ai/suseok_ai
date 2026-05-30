from __future__ import annotations

"""Legacy PyQt/manual trading engine.

The 64bit Core must use `/api/orders/enqueue` and GatewayCommand records for
real orders. This direct broker-client path remains only for the deprecated
PyQt app.
"""

from typing import Protocol

from trading.broker.models import BrokerExecutionEvent, BrokerOrderRequest, BrokerOrderResult
from storage.db import TradingDatabase
from trading.models import LegStatus, PriceTick, WatchItem
from trading.rules import (
    calculate_order_quantity,
    calculate_take_profit_quantity,
    is_within_ticks,
    reached_stop_loss,
    reached_take_profit,
    validate_weights,
)


class ClientProtocol(Protocol):
    price_received: object
    order_result: object
    execution_received: object
    message_received: object

    def send_order(self, request: BrokerOrderRequest) -> BrokerOrderResult: ...

    def register_realtime(self, codes) -> None: ...

    def get_code_name(self, code: str) -> str: ...


class TradingEngine:
    def __init__(self, client: ClientProtocol, db: TradingDatabase) -> None:
        self.client = client
        self.db = db
        self.account = ""
        self.ordering_enabled = False
        self.items: dict[str, WatchItem] = {item.code: item for item in db.load_watch_items()}
        self.log_handlers: list[callable] = []
        self.item_handlers: list[callable] = []
        self.order_handlers: list[callable] = []
        self.alert_handlers: list[callable] = []
        self._stop_loss_alerted_codes: set[str] = set()

        self.client.price_received.connect(self.on_price)
        self.client.order_result.connect(self.on_order_result)
        self.client.execution_received.connect(self.on_execution)
        self.client.message_received.connect(self.log)

    def set_account(self, account: str) -> None:
        self.account = account

    def set_ordering_enabled(self, enabled: bool) -> None:
        self.ordering_enabled = enabled
        self.log(f"주문 가능 상태: {'ON' if enabled else 'OFF'}")

    def add_or_update_item(self, item: WatchItem) -> tuple[bool, str]:
        ok, message = self.validate_item(item)
        if not ok:
            return False, message
        self.items[item.code] = item
        self.db.save_watch_item(item)
        self.emit_item_changed(item)
        self.register_realtime()
        return True, "저장했습니다."

    def remove_item(self, code: str) -> None:
        self.items.pop(code, None)
        self.db.delete_watch_item(code)
        self.register_realtime()

    def cancel_leg_order(self, code: str, leg_index: int) -> tuple[bool, str]:
        item = self.items.get(code)
        if not item:
            return False, "선택된 종목이 없습니다."
        if not self.account:
            return False, "계좌가 선택되지 않았습니다."
        leg = item.leg(leg_index)
        if not leg.order_no:
            return False, "취소할 주문번호가 아직 수신되지 않았습니다."
        remaining = max(0, leg.ordered_quantity - leg.filled_quantity)
        if remaining <= 0:
            return False, "취소할 미체결 수량이 없습니다."
        result = self.client.cancel_order(
            account=self.account,
            code=item.code,
            quantity=remaining,
            original_order_no=leg.order_no,
            tag=f"CANCEL{leg.index}_{item.code}",
        )
        self.log(f"{item.code} {leg.index}차 취소 요청: {result.message}")
        return result.ok, result.message

    def modify_leg_order(self, code: str, leg_index: int, new_price: int) -> tuple[bool, str]:
        item = self.items.get(code)
        if not item:
            return False, "선택된 종목이 없습니다."
        if not self.account:
            return False, "계좌가 선택되지 않았습니다."
        leg = item.leg(leg_index)
        if not leg.order_no:
            return False, "정정할 주문번호가 아직 수신되지 않았습니다."
        remaining = max(0, leg.ordered_quantity - leg.filled_quantity)
        if remaining <= 0:
            return False, "정정할 미체결 수량이 없습니다."
        result = self.client.modify_buy_order(
            account=self.account,
            code=item.code,
            quantity=remaining,
            price=new_price,
            original_order_no=leg.order_no,
            tag=f"MODBUY{leg.index}_{item.code}",
        )
        if result.ok:
            leg.target_price = new_price
            self.db.save_watch_item(item)
            self.emit_item_changed(item)
        self.log(f"{item.code} {leg.index}차 정정 요청: {result.message}")
        return result.ok, result.message

    def validate_item(self, item: WatchItem) -> tuple[bool, str]:
        if not item.code:
            return False, "종목코드가 필요합니다."
        if item.auto_buy_enabled and not item.stop_loss_price:
            return False, "자동매수 활성화 종목은 손절가가 필요합니다."
        if item.budget <= 0:
            return False, "종목별 총투입예정금이 필요합니다."
        ok, message = validate_weights([leg.weight_percent for leg in item.legs])
        if not ok:
            return False, message
        for leg in item.legs:
            if leg.weight_percent > 0 and leg.target_price <= 0:
                return False, f"{leg.index}차 목표매수가가 필요합니다."
            qty = calculate_order_quantity(item.budget, leg.weight_percent, leg.target_price)
            if leg.weight_percent > 0 and qty <= 0:
                return False, f"{leg.index}차 주문수량이 1주 미만입니다."
        return True, ""

    def register_realtime(self) -> None:
        codes = list(self.items)
        if codes:
            self.client.register_realtime(codes)

    def on_price(self, code: str, price: int, change_rate: float = 0.0, volume: int = 0, best_ask: int = 0, best_bid: int = 0) -> None:
        item = self.items.get(code)
        if not item:
            return
        tick = PriceTick(code, abs(int(price)), change_rate, volume, abs(int(best_ask)), abs(int(best_bid)))
        item.current_price = tick.price
        self.emit_item_changed(item)
        self._handle_stop_loss(item)
        self._handle_buy_triggers(item)
        self._handle_take_profit(item)

    def _handle_buy_triggers(self, item: WatchItem) -> None:
        if not self.ordering_enabled or not item.auto_buy_enabled:
            return
        if not self.account:
            self.log("계좌가 선택되지 않아 매수 주문을 보류합니다.")
            return
        ok, message = self.validate_item(item)
        if not ok:
            self.log(f"{item.code} 매수 조건 오류: {message}")
            return
        for leg in item.legs:
            if leg.status in {LegStatus.ORDER_SENT, LegStatus.UNFILLED, LegStatus.PARTIALLY_FILLED, LegStatus.FILLED}:
                continue
            if leg.weight_percent <= 0 or leg.target_price <= 0:
                continue
            leg.status = LegStatus.WATCHING
            if is_within_ticks(item.current_price, leg.target_price, item.tick_threshold):
                quantity = calculate_order_quantity(item.budget, leg.weight_percent, leg.target_price)
                request = BrokerOrderRequest(
                    account=self.account,
                    code=item.code,
                    quantity=quantity,
                    price=leg.target_price,
                    side="buy",
                    tag=f"BUY{leg.index}_{item.code}",
                    order_type=1,
                )
                leg.status = LegStatus.ORDER_SENT
                leg.ordered_quantity = quantity
                self.db.save_watch_item(item)
                self.client.send_order(request)
                self.log(f"{item.code} {leg.index}차 지정가 매수 전송: {quantity}주 @ {leg.target_price:,}")
                self.emit_item_changed(item)

    def _handle_take_profit(self, item: WatchItem) -> None:
        if not self.ordering_enabled or not item.auto_sell_enabled or item.take_profit_done:
            return
        if not self.account or item.holding_quantity <= 0:
            return
        if not reached_take_profit(item.current_price, item.average_price, item.take_profit_rate):
            return
        quantity = calculate_take_profit_quantity(item.holding_quantity, item.take_profit_sell_percent)
        if quantity <= 0:
            return
        request = BrokerOrderRequest(
            account=self.account,
            code=item.code,
            quantity=quantity,
            price=item.current_price,
            side="sell",
            tag=f"TP_{item.code}",
            order_type=2,
        )
        item.take_profit_done = True
        self.db.save_watch_item(item)
        self.client.send_order(request)
        self.log(f"{item.code} 익절 지정가 매도 전송: {quantity}주 @ {item.current_price:,}")
        self.emit_item_changed(item)

    def _handle_stop_loss(self, item: WatchItem) -> None:
        if reached_stop_loss(item.current_price, item.stop_loss_price):
            if item.code in self._stop_loss_alerted_codes:
                return
            self._stop_loss_alerted_codes.add(item.code)
            message = f"{item.code} 손절가 도달: 현재 {item.current_price:,} / 손절 {item.stop_loss_price:,}"
            self.log(message)
            self.alert(message)
        else:
            self._stop_loss_alerted_codes.discard(item.code)

    def on_order_result(self, result: BrokerOrderResult) -> None:
        request = result.request
        item = self.items.get(request.code)
        self.order_handlers_call(result)
        if not item:
            return
        if result.ok:
            if request.side == "buy":
                leg = self._leg_from_tag(item, request.tag)
                if leg:
                    leg.status = LegStatus.UNFILLED
            self.db.save_order_result(result)
        else:
            if request.side == "buy":
                leg = self._leg_from_tag(item, request.tag)
                if leg:
                    leg.status = LegStatus.WAITING
            self.log(f"{request.code} 주문 실패: {result.message}")
        self.db.save_watch_item(item)
        self.emit_item_changed(item)

    def on_execution(self, event: BrokerExecutionEvent) -> None:
        item = self.items.get(event.code)
        self.db.save_execution(event)
        if not item:
            return
        if event.side == "buy":
            leg = self._leg_from_event(item, event)
            if leg and event.order_no:
                leg.order_no = event.order_no
                if event.remaining_quantity > 0 and leg.status == LegStatus.ORDER_SENT:
                    leg.status = LegStatus.UNFILLED
        if event.filled_quantity <= 0:
            self.db.save_watch_item(item)
            self.emit_item_changed(item)
            return
        if event.side == "buy":
            previous_cost = item.average_price * item.holding_quantity
            fill_cost = event.price * event.filled_quantity
            item.holding_quantity += event.filled_quantity
            item.average_price = (previous_cost + fill_cost) / item.holding_quantity
            leg = self._leg_from_event(item, event)
            if leg:
                leg.filled_quantity += event.filled_quantity
                leg.order_no = event.order_no
                leg.status = LegStatus.FILLED if event.remaining_quantity == 0 else LegStatus.PARTIALLY_FILLED
        elif event.side == "sell":
            item.holding_quantity = max(0, item.holding_quantity - event.filled_quantity)
            if item.holding_quantity == 0:
                item.average_price = 0.0
        self.db.save_watch_item(item)
        self.emit_item_changed(item)
        self.log(f"{event.code} 체결: {event.side} {event.filled_quantity}주 @ {event.price:,}")

    def _leg_from_tag(self, item: WatchItem, tag: str):
        if not tag.startswith("BUY"):
            return None
        try:
            index = int(tag[3])
            return item.leg(index)
        except (ValueError, KeyError, IndexError):
            return None

    def _leg_from_event(self, item: WatchItem, event: BrokerExecutionEvent):
        leg = self._leg_from_tag(item, event.tag)
        if leg:
            return leg
        if event.order_no:
            for candidate in item.legs:
                if candidate.order_no == event.order_no:
                    return candidate
        for candidate in item.legs:
            if candidate.status in {LegStatus.ORDER_SENT, LegStatus.UNFILLED, LegStatus.PARTIALLY_FILLED}:
                return candidate
        return None

    def log(self, message: str) -> None:
        self.db.save_log(message)
        for handler in self.log_handlers:
            handler(message)

    def alert(self, message: str) -> None:
        for handler in self.alert_handlers:
            handler(message)

    def emit_item_changed(self, item: WatchItem) -> None:
        for handler in self.item_handlers:
            handler(item)

    def order_handlers_call(self, result: BrokerOrderResult) -> None:
        for handler in self.order_handlers:
            handler(result)
