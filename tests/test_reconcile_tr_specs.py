from trading.broker.reconcile_tr_models import ReconcileSourceType
from trading.broker.reconcile_tr_specs import KiwoomReconcileTrSpecRegistry


def test_reconcile_tr_spec_registry_declares_unverified_sources():
    registry = KiwoomReconcileTrSpecRegistry()

    open_orders = registry.get(ReconcileSourceType.OPEN_ORDERS)
    positions = registry.get(ReconcileSourceType.ACCOUNT_POSITIONS)
    cash = registry.get(ReconcileSourceType.ACCOUNT_CASH)

    assert open_orders.tr_code == "opt10075"
    assert positions.tr_code == "opw00018"
    assert cash.tr_code == "opw00001"
    assert open_orders.spec_validation_source == "SYNTHETIC"
    assert open_orders.spec_validation_status == "SYNTHETIC_ONLY"
    assert "비밀번호" in positions.sensitive_input_fields
    assert "비밀번호" in cash.sensitive_input_fields


def test_reconcile_specs_keep_password_out_of_non_sensitive_inputs():
    registry = KiwoomReconcileTrSpecRegistry()
    for spec in registry.list():
        for key, value in spec.input_fields.items():
            if key in spec.sensitive_input_fields:
                assert value == "credential_ref"
            else:
                assert "password" not in str(value).lower()
                assert "비밀번호" not in str(value)

