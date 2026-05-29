from kiwoom.client import MockKiwoomClient, OrderRequest
from trading.strategy.models import OrderMode
from trading.strategy.safety import ActualOrderGuard


def test_observe_guard_blocks_real_orders():
    guard = ActualOrderGuard()

    decision = guard.allow_real_order(
        OrderMode.OBSERVE,
        config_enabled=True,
        ui_enabled=True,
        account="1234567890",
        ordering_enabled=True,
    )

    assert not decision.allowed
    assert "OBSERVE_MODE" in decision.reason_codes
    assert "REAL_ORDER_DISABLED" in decision.reason_codes


def test_observe_safety_does_not_call_send_order(monkeypatch):
    client = MockKiwoomClient()
    guard = ActualOrderGuard()
    called = []

    def fail_send_order(request: OrderRequest):
        called.append(request)
        raise AssertionError("send_order must not be called in OBSERVE safety checks")

    monkeypatch.setattr(client, "send_order", fail_send_order)
    decision = guard.allow_real_order(OrderMode.OBSERVE)

    assert not decision.allowed
    assert called == []
    assert client.orders == []


def test_pr_1_4_modules_do_not_reference_real_order_path():
    for path in [
        "trading/strategy/indicators.py",
        "trading/strategy/intraday.py",
        "trading/strategy/market_index.py",
        "trading/strategy/themes.py",
        "trading/strategy/gates.py",
        "trading/strategy/pipeline.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "OrderRequest" not in source
        assert "send_order" not in source
        assert "EntryPlan" not in source
        assert "VirtualOrder" not in source


def test_pr_1_7_modules_do_not_reference_real_order_path():
    for path in [
        "trading/strategy/entry.py",
        "trading/strategy/virtual_orders.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "KiwoomClient" not in source
        assert "OrderRequest" not in source
        assert "send_order" not in source
        assert "VirtualPosition" not in source


def test_pr_1_8_exit_module_does_not_reference_real_order_path():
    source = open("trading/strategy/exit.py", encoding="utf-8").read()
    assert "KiwoomClient" not in source
    assert "OrderRequest" not in source
    assert "send_order" not in source


def test_pr_1_9_review_export_replay_paths_do_not_reference_real_order_path():
    for path in [
        "trading/strategy/review.py",
        "trading/strategy/export.py",
        "trading/strategy/replay.py",
        "trading/strategy/runtime.py",
        "trading/strategy/config.py",
        "ui/main_window.py",
        "main.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "OrderRequest" not in source
        assert "send_order" not in source
    for path in [
        "trading/strategy/review.py",
        "trading/strategy/export.py",
        "trading/strategy/replay.py",
        "trading/strategy/runtime.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "KiwoomClient" not in source


def test_pr_2_4_live_watch_paths_do_not_reference_real_order_path():
    for path in [
        "trading/strategy/bridge.py",
        "trading/strategy/realtime.py",
        "trading/strategy/holding.py",
        "trading/strategy/market_index.py",
    ]:
        source = open(path, encoding="utf-8").read()
        assert "OrderRequest" not in source
        assert "send_order" not in source


def test_review_tab_refresh_is_read_only_static_contract():
    source = open("ui/main_window.py", encoding="utf-8").read()
    start = source.index("    def refresh_review_table")
    end = source.index("    def _export_reviews_csv")
    refresh_source = source[start:end]

    assert "latest_trade_reviews" in refresh_source
    assert "save_trade_review" not in refresh_source
    assert "TradeReviewService" not in refresh_source
    assert "CandidateState" not in refresh_source
