from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Iterable

from trading.broker.models import GatewayEvent, utc_timestamp


PARSER_VERSION = "kiwoom_chejan_v2"


class ChejanParseStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class KiwoomChejanFid:
    fid: int
    name: str
    required_for: tuple[str, ...] = ()


class KiwoomChejanFidRegistry:
    def __init__(self, fids: Iterable[KiwoomChejanFid]) -> None:
        self._by_fid = {int(item.fid): item for item in fids}

    @classmethod
    def default(cls) -> "KiwoomChejanFidRegistry":
        return cls(DEFAULT_CHEJAN_FIDS)

    def name(self, fid: int) -> str:
        item = self._by_fid.get(int(fid))
        return item.name if item else f"fid_{int(fid)}"

    def known_fids_for_gubun(self, gubun: str) -> list[int]:
        group = "order" if str(gubun) == "0" else "balance" if str(gubun) == "1" else "special"
        result = [fid for fid, item in self._by_fid.items() if group in item.required_for]
        return sorted(set(result))

    def to_dict(self) -> dict[str, Any]:
        return {str(fid): asdict(item) for fid, item in sorted(self._by_fid.items())}


@dataclass(frozen=True)
class ChejanFieldValue:
    fid: int
    name: str
    field_present: bool
    raw_value: str
    parsed_value: Any = None
    warning_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChejanParseResult:
    gubun: str
    item_count: int
    requested_fids: list[int]
    present_fids: list[int]
    unknown_fids: list[int]
    raw_fids: dict[str, str]
    event_kind: str
    canonical_payload: dict[str, Any]
    parse_status: ChejanParseStatus | str
    missing_required_fields: list[str] = field(default_factory=list)
    warning_codes: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    broker_event_key: str = ""
    parser_version: str = PARSER_VERSION
    parsed_at: str = field(default_factory=utc_timestamp)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def gateway_event_type(self) -> str:
        if self.gubun == "0":
            return "kiwoom_order_chejan"
        if self.gubun == "1":
            return "kiwoom_balance_chejan"
        if self.gubun == "3":
            return "kiwoom_special_chejan"
        return "kiwoom_special_chejan"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["parse_status"] = self.parse_status.value if isinstance(self.parse_status, ChejanParseStatus) else str(self.parse_status)
        payload["gateway_event_type"] = self.gateway_event_type
        return payload

    def to_gateway_event(self, *, source: str = "kiwoom_gateway") -> GatewayEvent:
        payload = self.to_event_payload()
        return GatewayEvent(
            type=self.gateway_event_type,
            event_id=f"evt_chejan_{hashlib.sha256((self.broker_event_key or json.dumps(payload, sort_keys=True, default=str)).encode('utf-8')).hexdigest()[:24]}",
            payload=payload,
            source=source,
            timestamp=self.parsed_at,
        )

    def to_event_payload(self) -> dict[str, Any]:
        return {
            **dict(self.canonical_payload or {}),
            "gubun": self.gubun,
            "item_count": self.item_count,
            "requested_fids": list(self.requested_fids),
            "present_fids": list(self.present_fids),
            "unknown_fids": list(self.unknown_fids),
            "raw_fids": dict(self.raw_fids),
            "event_kind": self.event_kind,
            "parser_status": self.parse_status.value if isinstance(self.parse_status, ChejanParseStatus) else str(self.parse_status),
            "parser_warning_codes": list(self.warning_codes),
            "parser_error_codes": list(self.error_codes),
            "missing_required_fields": list(self.missing_required_fields),
            "broker_event_key": self.broker_event_key,
            "source": "KIWOOM_CHEJAN",
            "parser_version": self.parser_version,
            "parsed_at": self.parsed_at,
            "details": dict(self.details or {}),
        }


class KiwoomChejanRawReader:
    def __init__(
        self,
        get_value: Callable[[int], str],
        *,
        registry: KiwoomChejanFidRegistry | None = None,
    ) -> None:
        self.get_value = get_value
        self.registry = registry or KiwoomChejanFidRegistry.default()

    def read(self, *, gubun: str, fid_list: str) -> dict[str, str]:
        requested = parse_fid_list(fid_list)
        fids = sorted(set(requested + self.registry.known_fids_for_gubun(gubun)))
        result: dict[str, str] = {}
        for fid in fids:
            try:
                value = str(self.get_value(int(fid)) or "").strip()
            except Exception as exc:
                value = ""
                result[f"{fid}:read_error"] = str(exc)
            result[str(fid)] = value
        return {key: value for key, value in result.items() if not key.endswith(":read_error") or value}


class KiwoomChejanParser:
    def __init__(self, *, registry: KiwoomChejanFidRegistry | None = None) -> None:
        self.registry = registry or KiwoomChejanFidRegistry.default()

    @classmethod
    def from_env(cls) -> "KiwoomChejanParser":
        return cls()

    def parse(
        self,
        *,
        gubun: str,
        item_count: int,
        fid_list: str = "",
        raw_fids: dict[int | str, Any] | None = None,
    ) -> ChejanParseResult:
        raw = {str(key): str(value or "").strip() for key, value in dict(raw_fids or {}).items()}
        requested = parse_fid_list(fid_list)
        present = sorted(
            int(key)
            for key, value in raw.items()
            if str(key).isdigit() and (str(value) != "" or int(key) in requested)
        )
        unknown = sorted(fid for fid in present if self.registry.name(fid).startswith("fid_"))
        common = {
            "gubun": str(gubun),
            "item_count": int(item_count or 0),
            "requested_fids": requested,
            "present_fids": present,
            "unknown_fids": unknown,
            "raw_fids": raw,
        }
        if str(gubun) == "0":
            return KiwoomOrderChejanParser(self.registry).parse(**common)
        if str(gubun) == "1":
            return KiwoomBalanceChejanParser(self.registry).parse(**common)
        if str(gubun) == "3":
            return KiwoomSpecialSignalParser(self.registry).parse(**common)
        return ChejanParseResult(
            **common,
            event_kind="unsupported_gubun",
            canonical_payload={
                "source": "KIWOOM_CHEJAN",
                "parser_version": PARSER_VERSION,
                "raw_gubun": str(gubun),
                "raw_fids": raw,
            },
            parse_status=ChejanParseStatus.UNSUPPORTED,
            warning_codes=["UNSUPPORTED_GUBUN"],
            broker_event_key=_business_key("unsupported", str(gubun), _raw_checksum(raw)),
            details={"raw_gubun": str(gubun)},
        )


class KiwoomOrderChejanParser:
    def __init__(self, registry: KiwoomChejanFidRegistry) -> None:
        self.registry = registry

    def parse(self, **common: Any) -> ChejanParseResult:
        raw = dict(common["raw_fids"])
        field_details = _field_details(self.registry, raw, ORDER_FIELD_FIDS)
        warnings: list[str] = []
        errors: list[str] = []
        missing: list[str] = []
        account = _text(raw, 9201)
        order_no = _text(raw, 9203)
        code = _clean_code(_text(raw, 9001))
        name = _text(raw, 302)
        order_status = _text(raw, 913)
        order_gubun = _text(raw, 905)
        side_code = _text(raw, 907)
        reject_reason = _text(raw, 919)
        event_time = _text(raw, 908)
        execution_id = _text(raw, 909)
        order_quantity = _parse_int_field(self.registry, raw, 900)
        order_price = _parse_int_field(self.registry, raw, 901)
        unfilled_quantity = _parse_int_field(self.registry, raw, 902)
        cumulative_fill_amount = _parse_int_field(self.registry, raw, 903)
        execution_price = _parse_int_field(self.registry, raw, 910)
        execution_quantity = _parse_int_field(self.registry, raw, 911)
        unit_execution_price = _parse_int_field(self.registry, raw, 914)
        unit_execution_quantity = _parse_int_field(self.registry, raw, 915)
        if _env_bool("TRADING_KIWOOM_CHEJAN_REQUIRE_ACCOUNT", True) and not account:
            missing.append("account")
        if _env_bool("TRADING_KIWOOM_CHEJAN_REQUIRE_ORDER_NO", True) and not order_no:
            missing.append("order_no")
        if not code:
            missing.append("code")
        side = normalize_order_side(side_code=side_code, order_gubun=order_gubun)
        incremental_qty = _incremental_execution_quantity(execution_quantity.parsed_value, unit_execution_quantity)
        cumulative_qty = _cumulative_filled_quantity(
            order_quantity=order_quantity.parsed_value,
            unfilled_quantity=unfilled_quantity.parsed_value,
            execution_quantity=execution_quantity.parsed_value,
        )
        fill_status = _is_fill_status(order_status)
        fill_like = _has_positive(incremental_qty) or _has_positive(execution_quantity.parsed_value) or fill_status
        has_execution = bool(execution_id) or fill_like
        cancel_like = _is_cancel_status(order_status) or _is_cancel_order_gubun(order_gubun)
        reject_like = _is_reject_reason(reject_reason) or _is_reject_status(order_status)
        if has_execution:
            event_kind = "order_fill"
        elif reject_like:
            event_kind = "order_rejected"
        elif cancel_like:
            event_kind = "order_cancelled" if _value_or_zero(unfilled_quantity.parsed_value) <= 0 else "order_cancel_accepted"
        elif order_status:
            event_kind = "order_accepted"
        else:
            event_kind = "order_status_snapshot"
        for field in (order_quantity, order_price, unfilled_quantity):
            warnings.extend(field.warning_codes)
        if cumulative_fill_amount.field_present:
            warnings.extend(cumulative_fill_amount.warning_codes)
        if event_kind == "order_fill":
            for field in (execution_price, execution_quantity, unit_execution_price, unit_execution_quantity):
                warnings.extend(field.warning_codes)
        if fill_like and not execution_id:
            warnings.append("EXECUTION_ID_MISSING")
        if common["item_count"] and len(common["requested_fids"]) and common["item_count"] != len(common["requested_fids"]):
            warnings.append("ITEM_COUNT_FID_LIST_MISMATCH")
        if common["unknown_fids"]:
            warnings.append("UNKNOWN_FID_PRESENT")
        status = ChejanParseStatus.OK
        if missing:
            status = ChejanParseStatus.INVALID
            errors.append("REQUIRED_FIELD_MISSING")
        elif warnings:
            status = ChejanParseStatus.DEGRADED
        broker_event_key = _order_broker_event_key(
            account=account,
            order_no=order_no,
            original_order_no=_text(raw, 904),
            order_status=order_status,
            unfilled_quantity=unfilled_quantity.parsed_value,
            cumulative_filled_quantity=cumulative_qty,
            event_time=event_time,
            execution_id=execution_id,
            incremental_execution_quantity=incremental_qty,
            execution_price=execution_price.parsed_value,
            event_kind=event_kind,
        )
        payload = {
            "account": account,
            "code": code,
            "name": name,
            "order_no": order_no,
            "original_order_no": _text(raw, 904),
            "order_status": order_status,
            "order_business_type": _text(raw, 912),
            "order_gubun": order_gubun,
            "trade_gubun": _text(raw, 906),
            "side": side,
            "side_code": side_code,
            "order_quantity": order_quantity.parsed_value,
            "order_price": order_price.parsed_value,
            "unfilled_quantity": unfilled_quantity.parsed_value,
            "remaining_quantity": unfilled_quantity.parsed_value,
            "cumulative_filled_quantity": cumulative_qty,
            "cumulative_fill_amount": cumulative_fill_amount.parsed_value,
            "execution_id": execution_id,
            "execution_price": execution_price.parsed_value,
            "price": execution_price.parsed_value or order_price.parsed_value,
            "execution_quantity": execution_quantity.parsed_value,
            "unit_execution_price": unit_execution_price.parsed_value,
            "unit_execution_quantity": unit_execution_quantity.parsed_value,
            "incremental_execution_quantity": incremental_qty,
            "filled_quantity": cumulative_qty,
            "quantity": order_quantity.parsed_value or cumulative_qty or incremental_qty,
            "event_time": event_time,
            "timestamp": _kiwoom_event_time(event_time),
            "reject_reason": reject_reason,
            "screen_no": _text(raw, 920),
            "legacy_tag": "",
            "tag_source": "UNAVAILABLE_FROM_CHEJAN",
            "command_id": "",
            "idempotency_key": "",
            "correlation_status": "NOT_FOUND",
            "correlation_confidence": 0.0,
            "broker_event_key": broker_event_key,
            "dedupe_confidence": "LOW" if fill_like and not execution_id else "HIGH",
            "gubun": "0",
            "raw_fids": raw,
            "parser_status": status.value,
            "parser_warning_codes": sorted(set(warnings)),
            "source": "KIWOOM_CHEJAN",
            "parser_version": PARSER_VERSION,
        }
        return ChejanParseResult(
            **common,
            event_kind=event_kind,
            canonical_payload=payload,
            parse_status=status,
            missing_required_fields=sorted(set(missing)),
            warning_codes=sorted(set(warnings)),
            error_codes=sorted(set(errors)),
            broker_event_key=broker_event_key,
            details={
                "fields": {key: value.to_dict() for key, value in field_details.items()},
                "quantity_semantics": {
                    "fid_911": "execution_quantity_raw_contract_requires_fixture_validation",
                    "fid_915": "unit_execution_quantity_raw_contract_requires_fixture_validation",
                    "incremental_execution_quantity_source": "fid_915" if unit_execution_quantity.field_present else "fid_911" if execution_quantity.field_present else "",
                    "cumulative_filled_quantity_source": "order_quantity_minus_unfilled_quantity" if order_quantity.field_present and unfilled_quantity.field_present else "fid_911_fallback" if execution_quantity.field_present else "",
                },
            },
        )


class KiwoomBalanceChejanParser:
    def __init__(self, registry: KiwoomChejanFidRegistry) -> None:
        self.registry = registry

    def parse(self, **common: Any) -> ChejanParseResult:
        raw = dict(common["raw_fids"])
        field_details = _field_details(self.registry, raw, BALANCE_FIELD_FIDS)
        warnings: list[str] = []
        errors: list[str] = []
        missing: list[str] = []
        account = _text(raw, 9201)
        code = _clean_code(_text(raw, 9001))
        if _env_bool("TRADING_KIWOOM_CHEJAN_REQUIRE_ACCOUNT", True) and not account:
            missing.append("account")
        if not code:
            missing.append("code")
        parsed_numbers = {
            "current_price": _parse_int_field(self.registry, raw, 10),
            "position_quantity": _parse_int_field(self.registry, raw, 930),
            "average_buy_price": _parse_int_field(self.registry, raw, 931),
            "total_buy_amount": _parse_int_field(self.registry, raw, 932),
            "orderable_quantity": _parse_int_field(self.registry, raw, 933),
            "intraday_net_buy_quantity": _parse_int_field(self.registry, raw, 945),
            "deposit": _parse_int_field(self.registry, raw, 951),
            "best_ask": _parse_int_field(self.registry, raw, 27),
            "best_bid": _parse_int_field(self.registry, raw, 28),
            "reference_price": _parse_int_field(self.registry, raw, 307),
            "profit_rate": _parse_float_field(self.registry, raw, 8019),
        }
        for field in parsed_numbers.values():
            warnings.extend(field.warning_codes)
        if common["unknown_fids"]:
            warnings.append("UNKNOWN_FID_PRESENT")
        status = ChejanParseStatus.OK
        if missing:
            status = ChejanParseStatus.INVALID
            errors.append("REQUIRED_FIELD_MISSING")
        elif warnings:
            status = ChejanParseStatus.DEGRADED
        broker_event_key = _business_key(
            "balance",
            account,
            code,
            parsed_numbers["position_quantity"].parsed_value,
            parsed_numbers["orderable_quantity"].parsed_value,
            parsed_numbers["average_buy_price"].parsed_value,
            _raw_checksum(raw),
        )
        payload = {
            "account": account,
            "code": code,
            "name": _text(raw, 302),
            "position_quantity": parsed_numbers["position_quantity"].parsed_value,
            "quantity": parsed_numbers["position_quantity"].parsed_value,
            "orderable_quantity": parsed_numbers["orderable_quantity"].parsed_value,
            "available_quantity": parsed_numbers["orderable_quantity"].parsed_value,
            "average_buy_price": parsed_numbers["average_buy_price"].parsed_value,
            "avg_price": parsed_numbers["average_buy_price"].parsed_value,
            "total_buy_amount": parsed_numbers["total_buy_amount"].parsed_value,
            "current_price": parsed_numbers["current_price"].parsed_value,
            "intraday_net_buy_quantity": parsed_numbers["intraday_net_buy_quantity"].parsed_value,
            "side_code": _text(raw, 946),
            "best_ask": parsed_numbers["best_ask"].parsed_value,
            "best_bid": parsed_numbers["best_bid"].parsed_value,
            "reference_price": parsed_numbers["reference_price"].parsed_value,
            "profit_rate": parsed_numbers["profit_rate"].parsed_value,
            "deposit": parsed_numbers["deposit"].parsed_value,
            "snapshot_scope": "SINGLE_CODE_DELTA",
            "full_account_snapshot": False,
            "positions": [
                {
                    "account": account,
                    "code": code,
                    "quantity": parsed_numbers["position_quantity"].parsed_value,
                    "available_quantity": parsed_numbers["orderable_quantity"].parsed_value,
                    "avg_price": parsed_numbers["average_buy_price"].parsed_value,
                }
            ],
            "broker_event_key": broker_event_key,
            "gubun": "1",
            "raw_fids": raw,
            "parser_status": status.value,
            "parser_warning_codes": sorted(set(warnings)),
            "source": "KIWOOM_CHEJAN",
            "parser_version": PARSER_VERSION,
        }
        return ChejanParseResult(
            **common,
            event_kind="position_delta",
            canonical_payload=payload,
            parse_status=status,
            missing_required_fields=sorted(set(missing)),
            warning_codes=sorted(set(warnings)),
            error_codes=sorted(set(errors)),
            broker_event_key=broker_event_key,
            details={"fields": {key: value.to_dict() for key, value in field_details.items()}},
        )


class KiwoomSpecialSignalParser:
    def __init__(self, registry: KiwoomChejanFidRegistry) -> None:
        self.registry = registry

    def parse(self, **common: Any) -> ChejanParseResult:
        raw = dict(common["raw_fids"])
        return ChejanParseResult(
            **common,
            event_kind="special_signal",
            canonical_payload={
                "source": "KIWOOM_CHEJAN",
                "parser_version": PARSER_VERSION,
                "diagnostic_only": True,
                "raw_fids": raw,
            },
            parse_status=ChejanParseStatus.UNSUPPORTED,
            warning_codes=["SPECIAL_SIGNAL_IGNORED"],
            broker_event_key=_business_key("special", "3", _raw_checksum(raw)),
        )


@dataclass
class ChejanParserMetrics:
    enabled: bool = True
    parser_version: str = PARSER_VERSION
    total_event_count: int = 0
    order_event_count: int = 0
    balance_event_count: int = 0
    special_event_count: int = 0
    unsupported_gubun_count: int = 0
    parse_ok_count: int = 0
    parse_degraded_count: int = 0
    parse_invalid_count: int = 0
    missing_required_field_count: int = 0
    unknown_fid_count: int = 0
    execution_id_missing_count: int = 0
    correlation_exact_count: int = 0
    correlation_heuristic_count: int = 0
    correlation_ambiguous_count: int = 0
    correlation_not_found_count: int = 0
    last_gubun: str = ""
    last_event_kind: str = ""
    last_event_at: str = ""
    last_error: str = ""
    last_warning_codes: tuple[str, ...] = ()
    actual_fixture_validation_status: str = "NOT_RUN"

    def observe(self, result: ChejanParseResult) -> None:
        self.total_event_count += 1
        self.last_gubun = result.gubun
        self.last_event_kind = result.event_kind
        self.last_event_at = result.parsed_at
        self.last_warning_codes = tuple(result.warning_codes)
        if result.gubun == "0":
            self.order_event_count += 1
        elif result.gubun == "1":
            self.balance_event_count += 1
        elif result.gubun == "3":
            self.special_event_count += 1
        else:
            self.unsupported_gubun_count += 1
        status = str(result.parse_status.value if isinstance(result.parse_status, ChejanParseStatus) else result.parse_status)
        if status == ChejanParseStatus.OK.value:
            self.parse_ok_count += 1
        elif status == ChejanParseStatus.DEGRADED.value:
            self.parse_degraded_count += 1
        elif status == ChejanParseStatus.INVALID.value:
            self.parse_invalid_count += 1
            self.last_error = ",".join(result.error_codes)
        self.missing_required_field_count += len(result.missing_required_fields)
        self.unknown_fid_count += len(result.unknown_fids)
        if "EXECUTION_ID_MISSING" in result.warning_codes:
            self.execution_id_missing_count += 1
        correlation = str(result.canonical_payload.get("correlation_status") or "NOT_FOUND")
        if correlation == "EXACT":
            self.correlation_exact_count += 1
        elif correlation == "UNIQUE_HEURISTIC":
            self.correlation_heuristic_count += 1
        elif correlation == "AMBIGUOUS":
            self.correlation_ambiguous_count += 1
        else:
            self.correlation_not_found_count += 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ORDER_FIELD_FIDS = (9201, 9203, 9001, 912, 913, 302, 900, 901, 902, 903, 904, 905, 906, 907, 908, 909, 910, 911, 914, 915, 919, 920)
BALANCE_FIELD_FIDS = (9201, 9001, 302, 10, 930, 931, 932, 933, 945, 946, 951, 27, 28, 307, 8019)


DEFAULT_CHEJAN_FIDS = [
    KiwoomChejanFid(9201, "account", ("order", "balance")),
    KiwoomChejanFid(9203, "order_no", ("order",)),
    KiwoomChejanFid(9001, "code", ("order", "balance")),
    KiwoomChejanFid(912, "order_business_type", ("order",)),
    KiwoomChejanFid(913, "order_status", ("order",)),
    KiwoomChejanFid(302, "name", ("order", "balance")),
    KiwoomChejanFid(900, "order_quantity", ("order",)),
    KiwoomChejanFid(901, "order_price", ("order",)),
    KiwoomChejanFid(902, "unfilled_quantity", ("order",)),
    KiwoomChejanFid(903, "cumulative_fill_amount", ("order",)),
    KiwoomChejanFid(904, "original_order_no", ("order",)),
    KiwoomChejanFid(905, "order_gubun", ("order",)),
    KiwoomChejanFid(906, "trade_gubun", ("order",)),
    KiwoomChejanFid(907, "side_code", ("order",)),
    KiwoomChejanFid(908, "order_execution_time", ("order",)),
    KiwoomChejanFid(909, "execution_id", ("order",)),
    KiwoomChejanFid(910, "execution_price", ("order",)),
    KiwoomChejanFid(911, "execution_quantity", ("order",)),
    KiwoomChejanFid(914, "unit_execution_price", ("order",)),
    KiwoomChejanFid(915, "unit_execution_quantity", ("order",)),
    KiwoomChejanFid(919, "reject_reason", ("order",)),
    KiwoomChejanFid(920, "screen_no", ("order",)),
    KiwoomChejanFid(10, "current_price", ("balance",)),
    KiwoomChejanFid(930, "position_quantity", ("balance",)),
    KiwoomChejanFid(931, "average_buy_price", ("balance",)),
    KiwoomChejanFid(932, "total_buy_amount", ("balance",)),
    KiwoomChejanFid(933, "orderable_quantity", ("balance",)),
    KiwoomChejanFid(945, "intraday_net_buy_quantity", ("balance",)),
    KiwoomChejanFid(946, "buy_sell_code", ("balance",)),
    KiwoomChejanFid(951, "deposit", ("balance",)),
    KiwoomChejanFid(27, "best_ask", ("balance",)),
    KiwoomChejanFid(28, "best_bid", ("balance",)),
    KiwoomChejanFid(307, "reference_price", ("balance",)),
    KiwoomChejanFid(8019, "profit_rate", ("balance",)),
]


def parse_fid_list(fid_list: str) -> list[int]:
    result: list[int] = []
    for token in str(fid_list or "").replace(",", ";").split(";"):
        text = token.strip()
        if not text:
            continue
        try:
            result.append(int(text))
        except ValueError:
            continue
    return result


def normalize_order_side(*, side_code: str = "", order_gubun: str = "") -> str:
    side_text = f"{side_code} {order_gubun}".upper()
    if str(side_code).strip() == "2" or _contains_any(order_gubun, ("매수",)) or "BUY" in side_text:
        return "BUY"
    if str(side_code).strip() == "1" or _contains_any(order_gubun, ("매도",)) or "SELL" in side_text:
        return "SELL"
    return ""


def _field_details(registry: KiwoomChejanFidRegistry, raw: dict[str, str], fids: Iterable[int]) -> dict[str, ChejanFieldValue]:
    result: dict[str, ChejanFieldValue] = {}
    for fid in fids:
        text = _text(raw, fid)
        result[registry.name(fid)] = ChejanFieldValue(
            fid=fid,
            name=registry.name(fid),
            field_present=str(fid) in raw,
            raw_value=text,
            parsed_value=text if text != "" else None,
            warning_codes=tuple(["FIELD_MISSING"] if str(fid) not in raw or text == "" else []),
        )
    return result


def _parse_int_field(registry: KiwoomChejanFidRegistry, raw: dict[str, str], fid: int) -> ChejanFieldValue:
    present = str(fid) in raw
    text = _text(raw, fid)
    if not present or text == "":
        return ChejanFieldValue(fid, registry.name(fid), present, text, None, ("FIELD_MISSING",))
    cleaned = text.replace(",", "").replace("+", "").strip()
    try:
        return ChejanFieldValue(fid, registry.name(fid), present, text, abs(int(float(cleaned))), ())
    except (TypeError, ValueError):
        return ChejanFieldValue(fid, registry.name(fid), present, text, None, ("NUMERIC_PARSE_FAILED",))


def _parse_float_field(registry: KiwoomChejanFidRegistry, raw: dict[str, str], fid: int) -> ChejanFieldValue:
    present = str(fid) in raw
    text = _text(raw, fid)
    if not present or text == "":
        return ChejanFieldValue(fid, registry.name(fid), present, text, None, ("FIELD_MISSING",))
    cleaned = text.replace(",", "").replace("+", "").strip()
    try:
        return ChejanFieldValue(fid, registry.name(fid), present, text, float(cleaned), ())
    except (TypeError, ValueError):
        return ChejanFieldValue(fid, registry.name(fid), present, text, None, ("NUMERIC_PARSE_FAILED",))


def _text(raw: dict[str, str], fid: int) -> str:
    return str(raw.get(str(fid), "") or "").strip()


def _clean_code(value: str) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _incremental_execution_quantity(execution_quantity: Any, unit_execution_quantity: ChejanFieldValue) -> int | None:
    if unit_execution_quantity.field_present and unit_execution_quantity.parsed_value is not None:
        return int(unit_execution_quantity.parsed_value)
    if execution_quantity is not None:
        return int(execution_quantity)
    return None


def _cumulative_filled_quantity(*, order_quantity: Any, unfilled_quantity: Any, execution_quantity: Any) -> int | None:
    if order_quantity is not None and unfilled_quantity is not None:
        return max(0, int(order_quantity) - int(unfilled_quantity))
    if execution_quantity is not None:
        return int(execution_quantity)
    return None


def _has_positive(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _value_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _contains_any(value: str, tokens: Iterable[str]) -> bool:
    text = str(value or "")
    return any(token and token in text for token in tokens)


def _is_fill_status(order_status: str) -> bool:
    text = str(order_status or "").upper()
    return _contains_any(text, ("체결", "FILLED", "EXECUTED"))


def _is_cancel_status(order_status: str) -> bool:
    text = str(order_status or "").upper()
    return _contains_any(text, ("취소", "CANCEL"))


def _is_cancel_order_gubun(order_gubun: str) -> bool:
    text = str(order_gubun or "").upper()
    return _contains_any(text, ("취소", "CANCEL"))


def _is_reject_status(order_status: str) -> bool:
    text = str(order_status or "").upper()
    return _contains_any(text, ("거부", "거절", "REJECT"))


def _is_reject_reason(reject_reason: str) -> bool:
    text = str(reject_reason or "").strip()
    if not text:
        return False
    normalized = text.replace(" ", "").replace("\t", "")
    return normalized not in {"0", "00", "000", "0000", "정상"}


def _order_broker_event_key(**kwargs: Any) -> str:
    if kwargs.get("execution_id"):
        return _business_key("fill", kwargs.get("account"), kwargs.get("order_no"), kwargs.get("execution_id"))
    if kwargs.get("event_kind") == "order_fill":
        return _business_key(
            "fill-fallback",
            kwargs.get("account"),
            kwargs.get("order_no"),
            kwargs.get("event_time"),
            kwargs.get("incremental_execution_quantity"),
            kwargs.get("execution_price"),
            kwargs.get("cumulative_filled_quantity"),
        )
    return _business_key(
        "order-state",
        kwargs.get("account"),
        kwargs.get("order_no"),
        kwargs.get("original_order_no"),
        kwargs.get("order_status"),
        kwargs.get("unfilled_quantity"),
        kwargs.get("cumulative_filled_quantity"),
        kwargs.get("event_time"),
    )


def _business_key(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return f"kiwoom:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _raw_checksum(raw: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _kiwoom_event_time(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        event_time = now.replace(hour=int(text[:2]), minute=int(text[2:4]), second=int(text[4:6]), microsecond=0)
        return event_time.astimezone(timezone.utc).isoformat()
    return utc_timestamp()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = [
    "BALANCE_FIELD_FIDS",
    "ChejanFieldValue",
    "ChejanParseResult",
    "ChejanParseStatus",
    "ChejanParserMetrics",
    "KiwoomBalanceChejanParser",
    "KiwoomChejanFid",
    "KiwoomChejanFidRegistry",
    "KiwoomChejanParser",
    "KiwoomChejanRawReader",
    "KiwoomOrderChejanParser",
    "KiwoomSpecialSignalParser",
    "ORDER_FIELD_FIDS",
    "PARSER_VERSION",
    "normalize_order_side",
    "parse_fid_list",
]
