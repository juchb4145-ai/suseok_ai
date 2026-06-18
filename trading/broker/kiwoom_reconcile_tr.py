from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from trading.broker.models import GatewayCommand, new_message_id
from trading.broker.reconcile_tr_models import (
    BrokerCashSnapshot,
    BrokerOpenOrderSnapshot,
    BrokerPositionSnapshot,
    ReconcileParserStatus,
    ReconcileSourceType,
    ReconcileTrParseResult,
    account_token,
    payload_checksum,
)
from trading.broker.reconcile_tr_specs import KiwoomReconcileTrSpec, KiwoomReconcileTrSpecRegistry
from trading.strategy.candidates import normalize_code


BROKER_RECONCILE_PURPOSE = "broker_reconcile"


@dataclass(frozen=True)
class KiwoomCredentialLookupResult:
    available: bool
    password: str = ""
    reason: str = ""


class KiwoomCredentialProvider:
    def get_password(self, *, account: str, credential_ref: str = "") -> KiwoomCredentialLookupResult:
        raise NotImplementedError


class GatewayLocalCredentialProvider(KiwoomCredentialProvider):
    def __init__(self, env_name: str = "TRADING_KIWOOM_ACCOUNT_PASSWORD") -> None:
        self.env_name = env_name

    def get_password(self, *, account: str, credential_ref: str = "") -> KiwoomCredentialLookupResult:
        raw = os.getenv(str(credential_ref or self.env_name), "")
        if not raw:
            return KiwoomCredentialLookupResult(False, reason="CREDENTIAL_UNAVAILABLE")
        return KiwoomCredentialLookupResult(True, password=str(raw))


def build_reconcile_tr_command(
    *,
    account: str,
    logical_source: ReconcileSourceType | str,
    run_id: str,
    credential_ref: str = "TRADING_KIWOOM_ACCOUNT_PASSWORD",
    registry: KiwoomReconcileTrSpecRegistry | None = None,
    max_pages: int = 20,
) -> GatewayCommand:
    spec = (registry or KiwoomReconcileTrSpecRegistry()).get(logical_source)
    token = account_token(account)
    payload = {
        "purpose": BROKER_RECONCILE_PURPOSE,
        "reconcile_run_id": str(run_id or ""),
        "logical_source": spec.source_value,
        "account": str(account or ""),
        "account_token": token,
        "credential_ref": str(credential_ref or ""),
        "tr_code": spec.tr_code,
        "rq_name": spec.rq_name,
        "screen_no": spec.screen_no,
        "response_mode": "capture_v2",
        "input_fields": dict(spec.input_fields),
        "single_fields": list(spec.single_fields),
        "multi_fields": list(spec.multi_fields),
        "max_pages": max(1, int(max_pages or 20)),
        "spec_version": spec.parser_version,
    }
    return GatewayCommand(
        type="tr_request",
        payload=payload,
        command_id=new_message_id("cmd_reconcile"),
        source="broker_reconcile",
        idempotency_key=f"reconcile:{token}:{run_id}:{spec.source_value}:0",
    )


class ReconcileTrParser:
    def __init__(self, registry: KiwoomReconcileTrSpecRegistry | None = None) -> None:
        self.registry = registry or KiwoomReconcileTrSpecRegistry()

    def parse_command_ack(self, payload: dict[str, Any]) -> ReconcileTrParseResult:
        raw = dict(payload or {})
        if str(raw.get("purpose") or "") != BROKER_RECONCILE_PURPOSE:
            raise ValueError("not a broker reconcile command ack")
        run_id = str(raw.get("reconcile_run_id") or "")
        source = str(raw.get("logical_source") or "")
        spec = self.registry.get(source)
        captured_single = dict(raw.get("captured_single") or raw.get("merged_single") or {})
        rows = [dict(row) for row in list(raw.get("captured_rows") or raw.get("merged_rows") or raw.get("tr_rows") or [])]
        complete = bool(raw.get("complete", True)) and not list(raw.get("parser_errors") or raw.get("errors") or [])
        return self.parse_capture(
            run_id=run_id,
            account=str(raw.get("account") or ""),
            spec=spec,
            single=captured_single,
            rows=rows,
            complete=complete,
            raw=raw,
            page_count=int(raw.get("page_count") or 0),
        )

    def parse_capture(
        self,
        *,
        run_id: str,
        account: str,
        spec: KiwoomReconcileTrSpec,
        single: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        complete: bool = True,
        raw: dict[str, Any] | None = None,
        page_count: int = 0,
    ) -> ReconcileTrParseResult:
        single = dict(single or {})
        rows = [dict(row) for row in list(rows or [])]
        warnings: list[str] = []
        errors: list[str] = []
        token = account_token(account)
        source = spec.source_value
        if not token:
            errors.append("ACCOUNT_MISSING")
        if not complete:
            errors.append("TR_CAPTURE_INCOMPLETE")
        if source == ReconcileSourceType.OPEN_ORDERS.value:
            orders = tuple(self._parse_open_order(row, spec=spec, account_token=token, run_id=run_id, warnings=warnings) for row in rows)
            return _result(run_id, source, spec, complete, rows, single, warnings, errors, open_orders=orders, page_count=page_count)
        if source == ReconcileSourceType.ACCOUNT_POSITIONS.value:
            positions = tuple(self._parse_position(row, spec=spec, account_token=token, run_id=run_id, warnings=warnings) for row in rows)
            return _result(run_id, source, spec, complete, rows, single, warnings, errors, positions=positions, page_count=page_count)
        if source == ReconcileSourceType.ACCOUNT_CASH.value:
            cash = self._parse_cash(single, spec=spec, account_token=token, run_id=run_id, warnings=warnings)
            return _result(run_id, source, spec, complete, rows, single, warnings, errors, cash=cash, page_count=page_count)
        errors.append(f"UNSUPPORTED_SOURCE:{source}")
        return _result(run_id, source, spec, complete, rows, single, warnings, errors, page_count=page_count)

    def _parse_open_order(
        self,
        row: dict[str, Any],
        *,
        spec: KiwoomReconcileTrSpec,
        account_token: str,
        run_id: str,
        warnings: list[str],
    ) -> BrokerOpenOrderSnapshot:
        meta = _field_metadata(row, spec)
        remaining = _int_alias(row, spec, "remaining_quantity", warnings)
        order_qty = _int_alias(row, spec, "order_quantity", warnings)
        filled = _int_alias(row, spec, "filled_quantity", warnings)
        if filled == 0 and order_qty > 0 and remaining >= 0:
            filled = max(0, order_qty - remaining)
        return BrokerOpenOrderSnapshot(
            account_token=account_token,
            order_no=_str_alias(row, spec, "order_no"),
            original_order_no=_str_alias(row, spec, "original_order_no"),
            code=normalize_code(_str_alias(row, spec, "code")),
            side=_normalize_side(_str_alias(row, spec, "side")),
            order_quantity=order_qty,
            order_price=_int_alias(row, spec, "order_price", warnings),
            filled_quantity=filled,
            remaining_quantity=max(0, remaining),
            order_status=_str_alias(row, spec, "order_status"),
            order_time=_str_alias(row, spec, "order_time"),
            source_run_id=run_id,
            field_metadata=meta,
        )

    def _parse_position(
        self,
        row: dict[str, Any],
        *,
        spec: KiwoomReconcileTrSpec,
        account_token: str,
        run_id: str,
        warnings: list[str],
    ) -> BrokerPositionSnapshot:
        return BrokerPositionSnapshot(
            account_token=account_token,
            code=normalize_code(_str_alias(row, spec, "code")),
            quantity=_int_alias(row, spec, "quantity", warnings),
            orderable_quantity=_int_alias(row, spec, "orderable_quantity", warnings),
            average_price=_float_alias(row, spec, "average_price", warnings),
            total_buy_amount=_float_alias(row, spec, "total_buy_amount", warnings),
            current_price=_float_alias(row, spec, "current_price", warnings),
            evaluation_amount=_float_alias(row, spec, "evaluation_amount", warnings),
            evaluation_pnl=_float_alias(row, spec, "evaluation_pnl", warnings),
            profit_rate=_float_alias(row, spec, "profit_rate", warnings),
            source_run_id=run_id,
            field_metadata=_field_metadata(row, spec),
        )

    def _parse_cash(
        self,
        single: dict[str, Any],
        *,
        spec: KiwoomReconcileTrSpec,
        account_token: str,
        run_id: str,
        warnings: list[str],
    ) -> BrokerCashSnapshot:
        return BrokerCashSnapshot(
            account_token=account_token,
            deposit=_int_alias(single, spec, "deposit", warnings),
            orderable_cash=_int_alias(single, spec, "orderable_cash", warnings),
            withdrawable_cash=_int_alias(single, spec, "withdrawable_cash", warnings),
            d1_estimated_deposit=_int_alias(single, spec, "d1_estimated_deposit", warnings),
            d2_estimated_deposit=_int_alias(single, spec, "d2_estimated_deposit", warnings),
            source_run_id=run_id,
            field_metadata=_field_metadata(single, spec),
        )


def _result(
    run_id: str,
    source: str,
    spec: KiwoomReconcileTrSpec,
    complete: bool,
    rows: list[dict[str, Any]],
    single: dict[str, Any],
    warnings: list[str],
    errors: list[str],
    *,
    open_orders: tuple[BrokerOpenOrderSnapshot, ...] = (),
    positions: tuple[BrokerPositionSnapshot, ...] = (),
    cash: BrokerCashSnapshot | None = None,
    page_count: int = 0,
) -> ReconcileTrParseResult:
    valid_empty = bool(complete and not errors and not rows and spec.valid_empty_allowed)
    status = ReconcileParserStatus.INVALID if errors else ReconcileParserStatus.VALID_EMPTY if valid_empty else ReconcileParserStatus.DEGRADED if warnings else ReconcileParserStatus.PASS
    raw = {"single": single, "rows": rows, "source": source}
    return ReconcileTrParseResult(
        run_id=run_id,
        logical_source=source,
        parser_version=spec.parser_version,
        parser_status=status,
        tr_code=spec.tr_code,
        rq_name=spec.rq_name,
        complete=bool(complete and not errors),
        valid_empty=valid_empty,
        page_count=max(1 if rows or single else 0, int(page_count or 0)),
        row_count=len(rows),
        open_orders=open_orders,
        positions=positions,
        cash=cash,
        warnings=tuple(dict.fromkeys(warnings)),
        errors=tuple(dict.fromkeys(errors)),
        raw_checksum=payload_checksum(raw),
    )


def _str_alias(row: dict[str, Any], spec: KiwoomReconcileTrSpec, normalized: str) -> str:
    for field in spec.field_aliases.get(normalized, ()):
        if field in row and str(row.get(field) or "").strip():
            return str(row.get(field) or "").strip()
    return ""


def _int_alias(row: dict[str, Any], spec: KiwoomReconcileTrSpec, normalized: str, warnings: list[str]) -> int:
    raw = _str_alias(row, spec, normalized)
    if raw == "":
        return 0
    try:
        return int(float(_clean_number(raw)))
    except (TypeError, ValueError):
        warnings.append(f"FIELD_PARSE_FAILED:{normalized}:{raw}")
        return 0


def _float_alias(row: dict[str, Any], spec: KiwoomReconcileTrSpec, normalized: str, warnings: list[str]) -> float:
    raw = _str_alias(row, spec, normalized)
    if raw == "":
        return 0.0
    try:
        return float(_clean_number(raw))
    except (TypeError, ValueError):
        warnings.append(f"FIELD_PARSE_FAILED:{normalized}:{raw}")
        return 0.0


def _field_metadata(row: dict[str, Any], spec: KiwoomReconcileTrSpec) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for normalized, aliases in spec.field_aliases.items():
        source = next((field for field in aliases if field in row), "")
        raw = row.get(source, "") if source else ""
        result[normalized] = {
            "source_field_name": source,
            "raw_value": raw,
            "field_present": bool(source),
            "parse_warning": "",
        }
    return result


def _clean_number(value: Any) -> str:
    return str(value or "").strip().replace(",", "").replace("+", "").replace("%", "")


def _normalize_side(value: str) -> str:
    text = str(value or "").upper()
    if "매도" in text or "SELL" in text:
        return "SELL"
    if "매수" in text or "BUY" in text:
        return "BUY"
    return text
