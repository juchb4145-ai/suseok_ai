from trading.broker.kiwoom_reconcile_tr import ReconcileTrParser
from trading.broker.reconcile_tr_models import ReconcileSourceType
from trading.broker.reconcile_tr_specs import KiwoomReconcileTrSpecRegistry


def test_reconcile_parser_normalizes_open_orders():
    registry = KiwoomReconcileTrSpecRegistry()
    spec = registry.get(ReconcileSourceType.OPEN_ORDERS)

    result = ReconcileTrParser(registry).parse_capture(
        run_id="run-1",
        account="1234567890",
        spec=spec,
        rows=[
            {
                "주문번호": "OID-1",
                "종목코드": "A005930",
                "주문구분": "+매수",
                "주문수량": "3",
                "주문가격": "70,000",
                "미체결수량": "2",
                "주문상태": "접수",
            }
        ],
    )

    assert result.parser_status == "PASS"
    order = result.open_orders[0]
    assert order.account_token.startswith("ACC_TOKEN_")
    assert order.code == "005930"
    assert order.side == "BUY"
    assert order.filled_quantity == 1
    assert order.remaining_quantity == 2


def test_reconcile_parser_valid_empty_open_orders_is_not_parser_failure():
    spec = KiwoomReconcileTrSpecRegistry().get(ReconcileSourceType.OPEN_ORDERS)

    result = ReconcileTrParser().parse_capture(run_id="run-empty", account="1234567890", spec=spec, rows=[], complete=True)

    assert result.valid_empty is True
    assert result.parser_status == "VALID_EMPTY"
    assert result.complete is True


def test_reconcile_parser_normalizes_position_zero_quantity():
    spec = KiwoomReconcileTrSpecRegistry().get(ReconcileSourceType.ACCOUNT_POSITIONS)

    result = ReconcileTrParser().parse_capture(
        run_id="run-pos",
        account="1234567890",
        spec=spec,
        rows=[{"종목번호": "A005930", "보유수량": "0", "매매가능수량": "0", "평균단가": "0"}],
    )

    assert result.parser_status == "PASS"
    position = result.positions[0]
    assert position.code == "005930"
    assert position.quantity == 0
    assert position.orderable_quantity == 0


def test_reconcile_parser_normalizes_cash_single_output():
    spec = KiwoomReconcileTrSpecRegistry().get(ReconcileSourceType.ACCOUNT_CASH)

    result = ReconcileTrParser().parse_capture(
        run_id="run-cash",
        account="1234567890",
        spec=spec,
        single={"예수금": "1,000,000", "주문가능금액": "800,000", "출금가능금액": "700,000"},
    )

    assert result.parser_status == "PASS"
    assert result.cash.deposit == 1_000_000
    assert result.cash.orderable_cash == 800_000

