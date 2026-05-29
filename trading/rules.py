from __future__ import annotations


def tick_size(price: int) -> int:
    price = abs(int(price))
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def ticks_between(price_a: int, price_b: int) -> int:
    low = min(abs(int(price_a)), abs(int(price_b)))
    high = max(abs(int(price_a)), abs(int(price_b)))
    if low == high:
        return 0
    ticks = 0
    cursor = low
    while cursor < high:
        cursor += tick_size(cursor)
        ticks += 1
        if ticks > 1_000_000:
            raise ValueError("tick distance is unexpectedly large")
    return ticks


def is_within_ticks(current_price: int, target_price: int, threshold_ticks: int) -> bool:
    if current_price <= 0 or target_price <= 0:
        return False
    return ticks_between(current_price, target_price) <= max(0, int(threshold_ticks))


def calculate_order_quantity(budget: int, weight_percent: float, target_price: int) -> int:
    if budget <= 0 or target_price <= 0 or weight_percent <= 0:
        return 0
    return int((budget * (weight_percent / 100.0)) // target_price)


def validate_weights(weights: list[float]) -> tuple[bool, str]:
    if any(weight < 0 for weight in weights):
        return False, "비중은 음수일 수 없습니다."
    total = sum(weights)
    if total <= 0:
        return False, "차수 비중 합계가 0보다 커야 합니다."
    if total > 100.0:
        return False, "차수 비중 합계가 100%를 초과합니다."
    return True, ""


def calculate_take_profit_quantity(holding_quantity: int, sell_percent: float) -> int:
    if holding_quantity <= 0 or sell_percent <= 0:
        return 0
    quantity = int(holding_quantity * (sell_percent / 100.0))
    return max(1, min(holding_quantity, quantity))


def reached_take_profit(current_price: int, average_price: float, take_profit_rate: float) -> bool:
    if current_price <= 0 or average_price <= 0:
        return False
    return ((current_price - average_price) / average_price) * 100.0 >= take_profit_rate


def reached_stop_loss(current_price: int, stop_loss_price: int) -> bool:
    return current_price > 0 and stop_loss_price > 0 and current_price <= stop_loss_price
