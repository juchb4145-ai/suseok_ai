from trading.broker.reconcile_tr_models import ReconcileSourceType
from trading.broker.reconcile_tr_specs import KiwoomReconcileTrSpecRegistry


def test_reconcile_tr_spec_registry_declares_source_contracts():
    registry = KiwoomReconcileTrSpecRegistry()

    open_orders = registry.get(ReconcileSourceType.OPEN_ORDERS)
    positions = registry.get(ReconcileSourceType.ACCOUNT_POSITIONS)
    cash = registry.get(ReconcileSourceType.ACCOUNT_CASH)

    assert open_orders.tr_code == "opt10075"
    assert positions.tr_code == "opw00018"
    assert cash.tr_code == "opw00001"
    assert open_orders.spec_validation_source == "KOA_STUDIO_SCREENSHOT"
    assert open_orders.spec_validation_status == "HOLD"
    assert open_orders.input_fields["거래소구분"] == "0"
    assert "거래소구분" in open_orders.multi_fields
    assert "SOR구분" in open_orders.multi_fields
    assert positions.spec_validation_source == "KOA_STUDIO_SCREENSHOT"
    assert positions.spec_validation_status == "HOLD"
    assert positions.input_fields["비밀번호"] == ""
    assert positions.input_fields["거래소구분"] == ""
    assert "비밀번호" not in positions.sensitive_input_fields
    assert "총대출금" in positions.single_fields
    assert "대출일" in positions.multi_fields
    assert cash.spec_validation_source == "KOA_STUDIO_SCREENSHOT"
    assert cash.spec_validation_status == "HOLD"
    assert cash.input_fields["비밀번호"] == ""
    assert cash.input_fields["비밀번호입력매체구분"] == "00"
    assert cash.input_fields["조회구분"] == "2"
    assert "비밀번호" not in cash.sensitive_input_fields
    assert "출력건수" in cash.single_fields
    assert "통화코드" in cash.multi_fields
    assert "d+4외화예수금" in cash.multi_fields
    assert cash.field_aliases["orderable_cash"] == ("주문가능금액", "주식증거금현금")


def test_reconcile_specs_keep_password_out_of_non_sensitive_inputs():
    registry = KiwoomReconcileTrSpecRegistry()
    for spec in registry.list():
        for key, value in spec.input_fields.items():
            if key in spec.sensitive_input_fields:
                assert value == "credential_ref"
            else:
                assert "password" not in str(value).lower()
                assert "비밀번호" not in str(value)
