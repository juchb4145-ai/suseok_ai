from dataclasses import dataclass

from apps.kiwoom_gateway import _execute_broker_reconcile_tr_capture
from trading.broker.kiwoom_reconcile_tr import build_reconcile_tr_command
from trading.broker.reconcile_tr_models import ReconcileSourceType
from trading_app.api import _sensitive_command_payload_path


def test_reconcile_command_contains_credential_ref_not_password():
    command = build_reconcile_tr_command(
        account="9876543210",
        logical_source=ReconcileSourceType.ACCOUNT_POSITIONS,
        run_id="run-sec",
        credential_ref="LOCAL_SECRET_REF",
    )

    payload = command.payload
    assert payload["credential_ref"] == "LOCAL_SECRET_REF"
    assert "password" not in str(payload).lower()
    assert "1357" not in str(payload)
    assert payload["account_token"].startswith("ACC_TOKEN_")


@dataclass
class FakeCaptureResult:
    complete: bool = True
    errors: list[str] = None
    warnings: list[str] = None
    page_count: int = 1
    request_count: int = 1
    prev_next_sequence: list[str] = None
    merged_single: dict = None
    merged_rows: list = None
    pages: list = None

    def __post_init__(self):
        self.errors = self.errors or []
        self.warnings = self.warnings or []
        self.prev_next_sequence = self.prev_next_sequence or ["0"]
        self.merged_single = self.merged_single or {"총매입금액": "0"}
        self.merged_rows = self.merged_rows or []
        self.pages = self.pages or []


class FakeRunner:
    def __init__(self):
        self.inputs = None

    def request_capture(self, **kwargs):
        self.inputs = kwargs["inputs"]
        return FakeCaptureResult()


def test_gateway_reconcile_capture_uses_password_only_inside_runner(monkeypatch):
    monkeypatch.setenv("LOCAL_SECRET_REF", "1357")
    command = build_reconcile_tr_command(
        account="9876543210",
        logical_source=ReconcileSourceType.ACCOUNT_POSITIONS,
        run_id="run-sec",
        credential_ref="LOCAL_SECRET_REF",
    )
    runner = FakeRunner()

    ack = _execute_broker_reconcile_tr_capture(object(), command, command.payload, tr_runner=runner)

    assert runner.inputs["비밀번호"] == "1357"
    assert "1357" not in str(ack)
    assert "credential_ref" not in str(ack)
    assert ack["purpose"] == "broker_reconcile"


def test_command_payload_sensitive_scanner_blocks_password_keys():
    assert _sensitive_command_payload_path({"inputs": {"비밀번호": "1234"}}) == "payload.inputs.비밀번호"
    assert _sensitive_command_payload_path({"account_token": "ACC_TOKEN_SAFE"}) == ""
