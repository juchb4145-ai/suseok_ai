from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository, legacy_strategy_runtime_settings
from trading_app.promotion_controller import (
    DATA_INSUFFICIENT_LABELS,
    FALSE_POSITIVE_LABELS,
    OPPORTUNITY_LOSS_LABELS,
    PromotionController,
    PromotionControllerConfig,
    PromotionEvidence,
    REALTIME_LOW_REASONS,
    RISK_CASE_LABELS,
    build_promotion_evidence,
    config_from_settings,
    normalize_stage,
    promotion_reason_detail,
    promotion_reason_details,
    realtime_bucket_from_row,
)


DEFAULT_PROMOTION_POLICY_ID = "theme_lab_realtime_reliability_gate"
MAX_PROMOTION_EVIDENCE_LIMIT = 10000
MAX_GROUP_PREVIEW_ITEMS = 4


class PromotionEvidenceAdapter:
    def __init__(self, db: TradingDatabase, *, config: Optional[PromotionControllerConfig] = None) -> None:
        self.db = db
        self.config = config or self._load_config()
        self.controller = PromotionController(config=self.config)

    def build_evidence(
        self,
        *,
        policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
        current_stage: Optional[str] = None,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> PromotionEvidence:
        evidence_limit = self._normalize_limit(limit)
        outcomes = self.db.list_strategy_decision_outcomes(
            trade_date=trade_date,
            horizon_sec=horizon_sec,
            window_sec=window_sec,
            limit=evidence_limit,
            offset=0,
        )
        intents = self.db.list_runtime_order_intents_for_analysis(
            trade_date=trade_date,
            include_rejected=True,
            include_duplicates=True,
            limit=evidence_limit,
            offset=0,
        )
        live_orders = self.db.list_live_sim_orders(
            trade_date=trade_date,
            limit=evidence_limit,
            offset=0,
        )
        return build_promotion_evidence(
            policy_id=policy_id or DEFAULT_PROMOTION_POLICY_ID,
            current_stage=current_stage or self.config.default_current_stage,
            decision_outcomes=outcomes,
            runtime_order_intents=intents,
            live_sim_orders=live_orders,
        )

    def evaluate(
        self,
        *,
        policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
        current_stage: Optional[str] = None,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        filters = self.filters(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        evidence = self.build_evidence(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        decision = self.controller.evaluate(evidence)
        stage_matrix = self.controller.stage_matrix(evidence)
        decision_payload = decision.to_dict()
        decision_payload["blocker_details"] = promotion_reason_details(decision.blockers)
        decision_payload["warning_details"] = promotion_reason_details(decision.warnings)
        return {
            "policy_id": evidence.policy_id,
            "current_stage": evidence.current_stage,
            "target_stage": decision.target_stage,
            "recommended_stage": decision.recommended_stage,
            "action": decision.action,
            "eligible": decision.eligible,
            "confidence": decision.confidence,
            "decision": decision_payload,
            "stage_matrix": stage_matrix,
            "evidence": evidence.to_dict(),
            "config": self.config.to_dict(),
            "filters": filters,
        }

    def matrix(
        self,
        *,
        policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
        current_stage: Optional[str] = None,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        evidence = self.build_evidence(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        return {
            "policy_id": evidence.policy_id,
            "stage_matrix": self.controller.stage_matrix(evidence),
            "evidence": evidence.to_dict(),
            "config": self.config.to_dict(),
            "filters": self.filters(
                policy_id=policy_id,
                current_stage=current_stage,
                trade_date=trade_date,
                window_sec=window_sec,
                horizon_sec=horizon_sec,
                limit=limit,
            ),
        }

    def drilldown(
        self,
        *,
        blocker: Optional[str] = None,
        policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
        current_stage: Optional[str] = None,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
        detail_limit: int = 30,
    ) -> dict[str, Any]:
        decision_payload = self.evaluate(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        rows = self._load_source_rows(
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        available = list(decision_payload.get("decision", {}).get("blockers") or [])
        selected = str(blocker or (available[0] if available else "") or "").upper()
        requested = [selected] if selected else available
        if not requested:
            requested = ["NO_BLOCKER"]
        max_items = min(200, max(1, int(detail_limit or 30)))
        sections = [self._drilldown_section(item, rows, decision_payload, max_items) for item in requested]
        return {
            "policy_id": decision_payload.get("policy_id") or policy_id or DEFAULT_PROMOTION_POLICY_ID,
            "selected_blocker": selected,
            "selected_blocker_detail": promotion_reason_detail(selected) if selected else {},
            "available_blockers": available,
            "available_blocker_details": promotion_reason_details(available),
            "decision": decision_payload.get("decision", {}),
            "evidence": decision_payload.get("evidence", {}),
            "sections": sections,
            "grouped_items": sections[0]["grouped_items"] if sections else [],
            "items": sections[0]["items"] if sections else [],
            "filters": {
                **dict(decision_payload.get("filters") or {}),
                "blocker": selected,
                "detail_limit": max_items,
            },
        }

    def filters(
        self,
        *,
        policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
        current_stage: Optional[str] = None,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "policy_id": policy_id or DEFAULT_PROMOTION_POLICY_ID,
            "current_stage": normalize_stage(current_stage or self.config.default_current_stage),
            "trade_date": trade_date or "",
            "window_sec": window_sec,
            "horizon_sec": horizon_sec,
            "limit": self._normalize_limit(limit),
        }

    def _normalize_limit(self, limit: Optional[int]) -> int:
        raw = limit if limit is not None else self.config.rolling_decision_limit
        try:
            value = int(raw or self.config.rolling_decision_limit)
        except (TypeError, ValueError):
            value = int(self.config.rolling_decision_limit)
        return min(MAX_PROMOTION_EVIDENCE_LIMIT, max(1, value))

    def _load_config(self) -> PromotionControllerConfig:
        try:
            settings = StrategyRuntimeSettingsRepository(self.db).load()
        except Exception:
            settings = legacy_strategy_runtime_settings()
        return config_from_settings(settings)

    def _load_source_rows(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, list[dict[str, Any]]]:
        evidence_limit = self._normalize_limit(limit)
        return {
            "outcomes": self.db.list_strategy_decision_outcomes(
                trade_date=trade_date,
                horizon_sec=horizon_sec,
                window_sec=window_sec,
                limit=evidence_limit,
                offset=0,
            ),
            "intents": self.db.list_runtime_order_intents_for_analysis(
                trade_date=trade_date,
                include_rejected=True,
                include_duplicates=True,
                limit=evidence_limit,
                offset=0,
            ),
            "live_orders": self.db.list_live_sim_orders(
                trade_date=trade_date,
                limit=evidence_limit,
                offset=0,
            ),
        }

    def _drilldown_section(
        self,
        blocker: str,
        rows: dict[str, list[dict[str, Any]]],
        decision_payload: dict[str, Any],
        detail_limit: int,
    ) -> dict[str, Any]:
        key = str(blocker or "NO_BLOCKER").upper()
        outcomes = list(rows.get("outcomes") or [])
        intents = list(rows.get("intents") or [])
        live_orders = list(rows.get("live_orders") or [])
        items: list[dict[str, Any]]
        if key in {"REALTIME_HIGH_RATIO_LOW"}:
            items = [_outcome_item(row) for row in outcomes if (_realtime_bucket(row) or "NO_DATA") != "HIGH"]
            items += [_intent_item(row) for row in intents if (_realtime_bucket(row) or "NO_DATA") != "HIGH"]
            items += [_live_order_item(row) for row in live_orders if (_realtime_bucket(row) or "NO_DATA") != "HIGH"]
        elif key in {"EXPECTANCY_BELOW_THRESHOLD"}:
            sorted_rows = sorted(outcomes, key=lambda row: _number(row.get("current_return_pct"), row.get("max_return_pct")) or 0.0)
            items = [_outcome_item(row) for row in sorted_rows]
        elif key in {"FALSE_POSITIVE_RATE_HIGH"}:
            items = [_outcome_item(row) for row in outcomes if _upper(row.get("outcome_label")) in FALSE_POSITIVE_LABELS]
        elif key in {"OPPORTUNITY_LOSS_RATE_HIGH"}:
            items = [_outcome_item(row) for row in outcomes if _upper(row.get("outcome_label")) in OPPORTUNITY_LOSS_LABELS]
        elif key in {"RISK_CASE_RATE_HIGH"}:
            items = [_outcome_item(row) for row in outcomes if _upper(row.get("outcome_label")) in RISK_CASE_LABELS]
        elif key in {"DATA_INSUFFICIENT_RATE_HIGH"}:
            items = [_outcome_item(row) for row in outcomes if _upper(row.get("outcome_label")) in DATA_INSUFFICIENT_LABELS]
        elif key in {"REALTIME_LOW_MISSED_RATE_HIGH"}:
            items = [
                _outcome_item(row)
                for row in outcomes
                if _upper(row.get("outcome_label")) in OPPORTUNITY_LOSS_LABELS
                and any(reason in REALTIME_LOW_REASONS for reason in _reason_codes(row))
            ]
        elif key in {"ORDER_ERROR_RATE_HIGH", "CONSECUTIVE_ORDER_ERRORS", "REAL_MICRO_REQUIRES_ZERO_ORDER_ERRORS"}:
            items = [_intent_item(row) for row in intents if _is_order_error(row)]
            items += [_live_order_item(row) for row in live_orders if _is_order_error(row)]
        elif key == "DUPLICATE_ORDER_DETECTED":
            items = [_intent_item(row) for row in intents if _is_duplicate_order(row)]
        elif key == "INSUFFICIENT_DRY_RUN_ORDERS":
            items = [_intent_item(row) for row in intents]
        elif key in {"INSUFFICIENT_LIVE_SIM_ORDERS", "INSUFFICIENT_FILL_SAMPLE"}:
            items = [_live_order_item(row) for row in live_orders]
        else:
            items = [_outcome_item(row) for row in outcomes]
        if not items and key != "NO_BLOCKER":
            items = [_outcome_item(row) for row in outcomes[:detail_limit]]
        limited = items[:detail_limit]
        groups = _group_drilldown_items(items)
        limited_groups = groups[:detail_limit]
        metrics = dict(decision_payload.get("decision", {}).get("metrics") or {})
        reason_detail = promotion_reason_detail(key)
        return {
            "blocker": key,
            "title": _blocker_title(key),
            "summary": {
                "matching_count": len(items),
                "shown_count": len(limited),
                "group_count": len(groups),
                "shown_group_count": len(limited_groups),
                "outcome_count": len(outcomes),
                "intent_count": len(intents),
                "live_order_count": len(live_orders),
                "metric_value": _blocker_metric_value(key, metrics),
                "blocker_detail": reason_detail,
                "explanation_ko": _blocker_explanation(key),
            },
            "grouped_items": limited_groups,
            "items": limited,
        }


def _outcome_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": "decision_outcome",
        "id": str(row.get("outcome_id") or row.get("decision_id") or ""),
        "decision_id": str(row.get("decision_id") or ""),
        "trade_date": str(row.get("trade_date") or ""),
        "event_at": str(row.get("evaluated_at") or row.get("decision_at") or ""),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "theme_name": str(row.get("theme_name") or ""),
        "gate_status": str(row.get("gate_status") or ""),
        "action_type": str(row.get("action_type") or ""),
        "outcome_label": str(row.get("outcome_label") or ""),
        "horizon_sec": _int_or_none(row.get("horizon_sec")),
        "current_return_pct": _number(row.get("current_return_pct"), row.get("return_pct")),
        "max_return_pct": _number(row.get("max_return_pct")),
        "max_drawdown_pct": _number(row.get("max_drawdown_pct")),
        "realtime_bucket": _realtime_bucket(row) or "NO_DATA",
        "reason_codes": _reason_codes(row)[:8],
        "summary": str(row.get("outcome_reason") or row.get("gate_reason") or row.get("reason_family") or ""),
    }


def _intent_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": "runtime_order_intent",
        "id": str(row.get("intent_id") or ""),
        "intent_id": str(row.get("intent_id") or ""),
        "trade_date": str(row.get("trade_date") or ""),
        "event_at": str(row.get("updated_at") or row.get("created_at") or ""),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "side": str(row.get("side") or ""),
        "order_phase": str(row.get("order_phase") or ""),
        "status": str(row.get("status") or ""),
        "reason": str(row.get("reason") or ""),
        "realtime_bucket": _realtime_bucket(row) or "NO_DATA",
        "reason_codes": _reason_codes(row)[:8],
        "summary": str(row.get("gate_reason") or row.get("reason") or ""),
    }


def _live_order_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": "live_sim_order",
        "id": str(row.get("order_intent_id") or row.get("broker_order_id") or ""),
        "intent_id": str(row.get("order_intent_id") or ""),
        "trade_date": str(row.get("trade_date") or ""),
        "event_at": str(row.get("updated_at") or row.get("submitted_at") or row.get("accepted_at") or row.get("rejected_at") or ""),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "side": str(row.get("side") or ""),
        "status": str(row.get("order_status") or row.get("status") or ""),
        "reason": str(row.get("broker_response_message") or ""),
        "realtime_bucket": _realtime_bucket(row) or "NO_DATA",
        "reason_codes": _reason_codes(row)[:8],
        "summary": str(row.get("broker_response_code") or row.get("broker_response_message") or ""),
    }


def _group_drilldown_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key = _group_key(item)
        if key not in groups:
            groups[key] = _new_group(key, item)
            order.append(key)
        _add_group_item(groups[key], item)
    return [_finalize_group(groups[key]) for key in order]


def _group_key(item: dict[str, Any]) -> str:
    code = str(item.get("code") or "").strip()
    if code:
        return f"code:{code}"
    return f"{item.get('source_type') or 'row'}:{item.get('id') or item.get('decision_id') or len(str(item))}"


def _new_group(key: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "code": str(item.get("code") or ""),
        "name": str(item.get("name") or ""),
        "theme_name": str(item.get("theme_name") or ""),
        "row_count": 0,
        "bucket_counts": Counter(),
        "outcome_counts": Counter(),
        "source_type_counts": Counter(),
        "action_type_counts": Counter(),
        "gate_status_counts": Counter(),
        "horizons_sec": [],
        "latest_event_at": "",
        "latest_summary": "",
        "current_return_pct": None,
        "max_return_pct": None,
        "max_drawdown_pct": None,
        "representative": dict(item),
        "items": [],
    }


def _add_group_item(group: dict[str, Any], item: dict[str, Any]) -> None:
    group["row_count"] += 1
    bucket = str(item.get("realtime_bucket") or "NO_DATA").upper()
    group["bucket_counts"].update([bucket])
    for counter_key, item_key in (
        ("outcome_counts", "outcome_label"),
        ("source_type_counts", "source_type"),
        ("action_type_counts", "action_type"),
        ("gate_status_counts", "gate_status"),
    ):
        value = str(item.get(item_key) or "").strip()
        if value:
            group[counter_key].update([value])
    horizon = _int_or_none(item.get("horizon_sec"))
    if horizon is not None and horizon not in group["horizons_sec"]:
        group["horizons_sec"].append(horizon)
    event_at = str(item.get("event_at") or "")
    if event_at >= str(group.get("latest_event_at") or ""):
        group["latest_event_at"] = event_at
        group["latest_summary"] = str(item.get("summary") or "")
    current_return = _number(item.get("current_return_pct"))
    if current_return is not None and (
        group.get("current_return_pct") is None or current_return < float(group.get("current_return_pct") or 0.0)
    ):
        group["current_return_pct"] = current_return
    max_return = _number(item.get("max_return_pct"))
    if max_return is not None and (
        group.get("max_return_pct") is None or max_return > float(group.get("max_return_pct") or 0.0)
    ):
        group["max_return_pct"] = max_return
    max_drawdown = _number(item.get("max_drawdown_pct"))
    if max_drawdown is not None and (
        group.get("max_drawdown_pct") is None or max_drawdown < float(group.get("max_drawdown_pct") or 0.0)
    ):
        group["max_drawdown_pct"] = max_drawdown
    if len(group["items"]) < MAX_GROUP_PREVIEW_ITEMS:
        group["items"].append(dict(item))


def _finalize_group(group: dict[str, Any]) -> dict[str, Any]:
    bucket_counts = dict(group["bucket_counts"])
    outcome_counts = dict(group["outcome_counts"])
    return {
        "key": group["key"],
        "code": group["code"],
        "name": group["name"],
        "theme_name": group["theme_name"],
        "row_count": group["row_count"],
        "realtime_bucket": _group_bucket(bucket_counts),
        "bucket_counts": bucket_counts,
        "outcome_counts": outcome_counts,
        "source_type_counts": dict(group["source_type_counts"]),
        "action_type_counts": dict(group["action_type_counts"]),
        "gate_status_counts": dict(group["gate_status_counts"]),
        "horizons_sec": sorted(group["horizons_sec"]),
        "latest_event_at": group["latest_event_at"],
        "latest_summary": group["latest_summary"],
        "representative_label": _top_counter_key(outcome_counts) or str(group["representative"].get("status") or ""),
        "current_return_pct": group["current_return_pct"],
        "max_return_pct": group["max_return_pct"],
        "max_drawdown_pct": group["max_drawdown_pct"],
        "items": group["items"],
    }


def _group_bucket(bucket_counts: dict[str, int]) -> str:
    for bucket in ("BROKEN", "LOW", "NO_DATA", "MEDIUM", "HIGH"):
        if int(bucket_counts.get(bucket) or 0) > 0:
            return bucket
    return "NO_DATA"


def _top_counter_key(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))[0][0]


def _blocker_title(blocker: str) -> str:
    return {
        "REALTIME_HIGH_RATIO_LOW": "Realtime reliability bucket contributors",
        "EXPECTANCY_BELOW_THRESHOLD": "Lowest return outcome contributors",
        "FALSE_POSITIVE_RATE_HIGH": "False-positive outcomes",
        "OPPORTUNITY_LOSS_RATE_HIGH": "Opportunity-loss outcomes",
        "RISK_CASE_RATE_HIGH": "Risk-case outcomes",
        "DATA_INSUFFICIENT_RATE_HIGH": "Data-insufficient outcomes",
        "REALTIME_LOW_MISSED_RATE_HIGH": "Realtime-low missed opportunity outcomes",
        "ORDER_ERROR_RATE_HIGH": "Order error rows",
        "CONSECUTIVE_ORDER_ERRORS": "Consecutive order error tail",
        "REAL_MICRO_REQUIRES_ZERO_ORDER_ERRORS": "Order errors blocking real micro",
        "DUPLICATE_ORDER_DETECTED": "Duplicate order rows",
        "INSUFFICIENT_DECISION_SAMPLE": "Recent decision outcome sample",
        "INSUFFICIENT_TRADE_DAYS": "Recent decision outcome sample",
        "INSUFFICIENT_DRY_RUN_ORDERS": "Dry-run order intent sample",
        "INSUFFICIENT_LIVE_SIM_ORDERS": "Live-sim order sample",
        "INSUFFICIENT_FILL_SAMPLE": "Live-sim fill sample",
        "NO_BLOCKER": "Promotion evidence sample",
    }.get(blocker, "Promotion evidence sample")


def _blocker_explanation(blocker: str) -> str:
    return {
        "REALTIME_HIGH_RATIO_LOW": "HIGH 버킷이 아닌 realtime evidence가 승급 품질을 낮추는지 확인합니다.",
        "EXPECTANCY_BELOW_THRESHOLD": "평균 기대수익률을 끌어내린 outcome을 낮은 수익률 순으로 보여줍니다.",
        "ORDER_ERROR_RATE_HIGH": "주문 거절/실패가 발생한 intent 또는 live-sim order를 확인합니다.",
        "REALTIME_LOW_MISSED_RATE_HIGH": "신뢰도 낮음 때문에 기다렸지만 이후 기회를 놓친 케이스를 확인합니다.",
        "INSUFFICIENT_DECISION_SAMPLE": "승급 판단에 필요한 decision outcome 샘플이 충분한지 확인합니다.",
        "INSUFFICIENT_TRADE_DAYS": "거래일 분산이 충분한지 확인합니다.",
    }.get(blocker, "해당 blocker와 관련된 최근 evidence rows를 확인합니다.")


def _blocker_metric_value(blocker: str, metrics: dict[str, Any]) -> Any:
    mapping = {
        "REALTIME_HIGH_RATIO_LOW": "realtime_high_ratio",
        "EXPECTANCY_BELOW_THRESHOLD": "avg_return_pct",
        "FALSE_POSITIVE_RATE_HIGH": "false_positive_rate",
        "OPPORTUNITY_LOSS_RATE_HIGH": "opportunity_loss_rate",
        "RISK_CASE_RATE_HIGH": "risk_case_rate",
        "DATA_INSUFFICIENT_RATE_HIGH": "data_insufficient_rate",
        "REALTIME_LOW_MISSED_RATE_HIGH": "realtime_low_missed_rate",
        "ORDER_ERROR_RATE_HIGH": "order_error_rate",
        "INSUFFICIENT_DECISION_SAMPLE": "decision_count",
        "INSUFFICIENT_TRADE_DAYS": "trade_day_count",
        "INSUFFICIENT_DRY_RUN_ORDERS": "order_count",
        "INSUFFICIENT_LIVE_SIM_ORDERS": "live_sim_order_count",
        "INSUFFICIENT_FILL_SAMPLE": "fill_count",
    }
    key = mapping.get(blocker)
    return metrics.get(key) if key else None


def _blocker_title(blocker: str) -> str:
    return "승격 근거 샘플" if blocker == "NO_BLOCKER" else promotion_reason_detail(blocker)["label_ko"]


def _blocker_explanation(blocker: str) -> str:
    return promotion_reason_detail(blocker)["description_ko"]


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata") or row.get("details") or row.get("request") or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _realtime_bucket(row: dict[str, Any]) -> str:
    return realtime_bucket_from_row(row)


def _reason_codes(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    metadata = _metadata(row)
    for source in (row, metadata):
        raw = source.get("reason_codes") or source.get("reason_codes_json") or []
        if isinstance(raw, str):
            values.extend(item.strip().upper() for item in raw.replace("[", "").replace("]", "").replace('"', "").split(",") if item.strip())
        elif isinstance(raw, (list, tuple, set)):
            values.extend(str(item).strip().upper() for item in raw if str(item).strip())
    for key in ("gate_reason", "reason", "primary_reason_code"):
        if row.get(key):
            values.append(str(row.get(key)).strip().upper())
    return list(dict.fromkeys(values))


def _is_order_error(row: dict[str, Any]) -> bool:
    status = _upper(row.get("status") or row.get("order_status"))
    return status in {"ERROR", "FAILED", "REJECTED", "DRY_RUN_REJECTED", "LIVE_BLOCKED", "CANCEL_FAILED"} or "ORDER_REJECTED" in _reason_codes(row)


def _is_duplicate_order(row: dict[str, Any]) -> bool:
    metadata = _metadata(row)
    return _upper(row.get("status")) == "DUPLICATE" or bool(row.get("duplicate")) or bool(metadata.get("duplicate")) or "DUPLICATE_ORDER" in _reason_codes(row)


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()
