from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading.broker.reconcile_tr_models import ReconcileSourceType, ReconcileTrValidationStatus


SPEC_VERSION = "kiwoom_reconcile_tr_specs_v1"


@dataclass(frozen=True)
class KiwoomReconcileTrSpec:
    logical_source: ReconcileSourceType | str
    tr_code: str
    rq_name: str
    screen_no: str
    input_fields: dict[str, str] = field(default_factory=dict)
    sensitive_input_fields: tuple[str, ...] = ()
    single_fields: tuple[str, ...] = ()
    multi_fields: tuple[str, ...] = ()
    field_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    supports_pagination: bool = False
    valid_empty_allowed: bool = False
    required_single_fields: tuple[str, ...] = ()
    required_multi_fields: tuple[str, ...] = ()
    parser_version: str = "kiwoom_reconcile_parser_v1"
    spec_validation_source: str = "SYNTHETIC"
    spec_validation_status: ReconcileTrValidationStatus | str = ReconcileTrValidationStatus.SYNTHETIC_ONLY
    notes: str = ""

    @property
    def source_value(self) -> str:
        return self.logical_source.value if isinstance(self.logical_source, ReconcileSourceType) else str(self.logical_source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_source": self.source_value,
            "tr_code": self.tr_code,
            "rq_name": self.rq_name,
            "screen_no": self.screen_no,
            "input_fields": dict(self.input_fields),
            "sensitive_input_fields": list(self.sensitive_input_fields),
            "single_fields": list(self.single_fields),
            "multi_fields": list(self.multi_fields),
            "field_aliases": {key: list(value) for key, value in self.field_aliases.items()},
            "supports_pagination": self.supports_pagination,
            "valid_empty_allowed": self.valid_empty_allowed,
            "required_single_fields": list(self.required_single_fields),
            "required_multi_fields": list(self.required_multi_fields),
            "parser_version": self.parser_version,
            "spec_validation_source": self.spec_validation_source,
            "spec_validation_status": (
                self.spec_validation_status.value
                if isinstance(self.spec_validation_status, ReconcileTrValidationStatus)
                else str(self.spec_validation_status)
            ),
            "notes": self.notes,
        }


class KiwoomReconcileTrSpecRegistry:
    def __init__(self, specs: list[KiwoomReconcileTrSpec] | None = None) -> None:
        self._specs = {spec.source_value: spec for spec in specs or default_specs()}

    def get(self, source: ReconcileSourceType | str) -> KiwoomReconcileTrSpec:
        key = source.value if isinstance(source, ReconcileSourceType) else str(source)
        if key not in self._specs:
            raise KeyError(f"unknown reconcile source: {key}")
        return self._specs[key]

    def list(self) -> list[KiwoomReconcileTrSpec]:
        return list(self._specs.values())

    def to_dict(self) -> dict[str, Any]:
        return {"spec_version": SPEC_VERSION, "specs": [spec.to_dict() for spec in self.list()]}


def default_specs() -> list[KiwoomReconcileTrSpec]:
    return [
        KiwoomReconcileTrSpec(
            logical_source=ReconcileSourceType.OPEN_ORDERS,
            tr_code="opt10075",
            rq_name="실시간미체결요청",
            screen_no="8711",
            input_fields={
                "계좌번호": "account",
                "전체종목구분": "0",
                "매매구분": "0",
                "종목코드": "",
                "체결구분": "1",
            },
            single_fields=(),
            multi_fields=(
                "계좌번호",
                "주문번호",
                "관리사번",
                "종목코드",
                "업무구분",
                "주문상태",
                "종목명",
                "주문수량",
                "주문가격",
                "미체결수량",
                "체결누계금액",
                "원주문번호",
                "주문구분",
                "매매구분",
                "시간",
                "체결번호",
                "체결가",
                "체결량",
                "현재가",
                "매도호가",
                "매수호가",
            ),
            field_aliases={
                "account": ("계좌번호",),
                "order_no": ("주문번호",),
                "original_order_no": ("원주문번호",),
                "code": ("종목코드",),
                "side": ("주문구분", "매매구분"),
                "order_quantity": ("주문수량",),
                "order_price": ("주문가격",),
                "remaining_quantity": ("미체결수량",),
                "filled_quantity": ("체결량", "체결누계수량"),
                "order_status": ("주문상태",),
                "order_time": ("시간",),
            },
            supports_pagination=True,
            valid_empty_allowed=True,
            required_multi_fields=("주문번호", "종목코드", "주문수량", "미체결수량"),
            notes="TR/FID names require KOA Studio or simulation capture validation before PASS.",
        ),
        KiwoomReconcileTrSpec(
            logical_source=ReconcileSourceType.ACCOUNT_POSITIONS,
            tr_code="opw00018",
            rq_name="계좌평가잔고내역요청",
            screen_no="8712",
            input_fields={
                "계좌번호": "account",
                "비밀번호": "credential_ref",
                "비밀번호입력매체구분": "00",
                "조회구분": "2",
            },
            sensitive_input_fields=("비밀번호",),
            single_fields=("총매입금액", "총평가금액", "총평가손익금액", "총수익률(%)", "추정예탁자산"),
            multi_fields=(
                "종목번호",
                "종목명",
                "보유수량",
                "매매가능수량",
                "평균단가",
                "매입가",
                "현재가",
                "평가금액",
                "평가손익",
                "수익률(%)",
            ),
            field_aliases={
                "code": ("종목번호", "종목코드"),
                "quantity": ("보유수량",),
                "orderable_quantity": ("매매가능수량", "주문가능수량"),
                "average_price": ("평균단가", "매입가"),
                "total_buy_amount": ("매입금액",),
                "current_price": ("현재가",),
                "evaluation_amount": ("평가금액",),
                "evaluation_pnl": ("평가손익",),
                "profit_rate": ("수익률(%)",),
            },
            supports_pagination=True,
            valid_empty_allowed=True,
            required_single_fields=("총매입금액",),
            required_multi_fields=("종목번호", "보유수량"),
            notes="Password is resolved only inside Gateway and never persisted in Core command payload.",
        ),
        KiwoomReconcileTrSpec(
            logical_source=ReconcileSourceType.ACCOUNT_CASH,
            tr_code="opw00001",
            rq_name="예수금상세현황요청",
            screen_no="8713",
            input_fields={
                "계좌번호": "account",
                "비밀번호": "credential_ref",
                "비밀번호입력매체구분": "00",
                "조회구분": "2",
            },
            sensitive_input_fields=("비밀번호",),
            single_fields=("예수금", "주문가능금액", "출금가능금액", "d+1추정예수금", "d+2추정예수금"),
            multi_fields=(),
            field_aliases={
                "deposit": ("예수금",),
                "orderable_cash": ("주문가능금액", "주식증거금현금"),
                "withdrawable_cash": ("출금가능금액",),
                "d1_estimated_deposit": ("d+1추정예수금", "D+1추정예수금"),
                "d2_estimated_deposit": ("d+2추정예수금", "D+2추정예수금"),
            },
            supports_pagination=False,
            valid_empty_allowed=False,
            required_single_fields=("예수금",),
            required_multi_fields=(),
            notes="Single output contract must be validated with simulation capture before PASS.",
        ),
    ]

