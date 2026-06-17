from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, new_message_id
from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.theme_engine.leadership import StockLeadershipRole
from trading.theme_engine.models import StockSnapshot, ThemeMembership, ThemeStatus
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.opening_burst import (
    OpeningBurstConfig,
    OpeningThemeBurstEngine,
    OpeningThemeBurstResult,
    OpeningTurnoverSeed,
    OpeningTurnoverSeedCollector,
)
from trading.theme_engine.repository import ThemeEngineRepository


OPENING_TURNOVER_SEED_PURPOSE = "opening_turnover_seed"
OPENING_TR_CODE = "opt10032"
OPENING_RQ_NAME = "OpeningTurnoverSeed_opt10032"
OPENING_SCREEN_NO = "8720"
OPENING_REALTIME_SCREEN_NO = "8721"
OPENING_BURST_OUTPUT_MODE = "OBSERVE"
KST = timezone(timedelta(hours=9), "KST")
REGULAR_OPEN = time(9, 0)
REGULAR_CLOSE = time(15, 30)


OPT10032_FIELDS = [
    "\uc885\ubaa9\ucf54\ub4dc",
    "\uc885\ubaa9\uba85",
    "\ud604\uc7ac\uac00",
    "\ub4f1\ub77d\ub960",
    "\uac70\ub798\ub300\uae08",
    "\uac70\ub798\ub7c9",
    "\uc21c\uc704",
]


@dataclass(frozen=True)
class OpeningBurstRuntimeConfig:
    enabled: bool = False
    observe_only: bool = True
    trading_mode: str = "OBSERVE"
    seed_times: tuple[str, ...] = ("09:03", "09:06", "09:09", "09:12", "09:15")
    top_n_per_call: int = 100
    max_union_size: int = 300
    max_realtime_register: int = 100
    tr_ttl_sec: int = 60
    register_ttl_sec: int = 60
    tr_screen_no: str = OPENING_SCREEN_NO
    realtime_screen_no: str = OPENING_REALTIME_SCREEN_NO

    @classmethod
    def from_env(cls, *, trading_mode: str | None = None) -> "OpeningBurstRuntimeConfig":
        default_seed_times = ("09:03", "09:06", "09:09", "09:12", "09:15")
        seed_times = _csv_env(
            "TRADING_OPENING_BURST_SEED_TIMES",
            default_seed_times,
        )
        parsed_seed_times = tuple(_valid_seed_time(value) for value in seed_times if _valid_seed_time(value))
        return cls(
            enabled=_bool_env("TRADING_OPENING_BURST_ENABLED", False),
            observe_only=_bool_env("TRADING_OPENING_BURST_OBSERVE_ONLY", True),
            trading_mode=str(trading_mode or os.environ.get("TRADING_MODE", "OBSERVE") or "OBSERVE").strip().upper()
            or "OBSERVE",
            seed_times=parsed_seed_times or default_seed_times,
            top_n_per_call=max(1, _int_env("TRADING_OPENING_BURST_TOP_N_PER_CALL", 100)),
            max_union_size=max(1, _int_env("TRADING_OPENING_BURST_MAX_UNION_SIZE", 300)),
            max_realtime_register=max(1, _int_env("TRADING_OPENING_BURST_MAX_REALTIME_REGISTER", 100)),
        )

    def engine_config(self) -> OpeningBurstConfig:
        return OpeningBurstConfig(
            seed_call_times=self.seed_times,
            top_n_per_call=self.top_n_per_call,
            max_union_size=self.max_union_size,
        )


@dataclass(frozen=True)
class ParsedOpeningSeedRow:
    seed: OpeningTurnoverSeed
    current_price: float = 0.0
    volume: int = 0
    parser_status: str = "OK"
    parser_missing_fields: tuple[str, ...] = ()

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.seed.stock_code,
            "stock_name": self.seed.stock_name,
            "rank": self.seed.seed_rank,
            "turnover_krw": self.seed.turnover_krw,
            "change_rate_pct": self.seed.change_rate_pct,
            "current_price": self.current_price,
            "volume": self.volume,
            "parser_status": self.parser_status,
            "parser_missing_fields": list(self.parser_missing_fields),
            "raw": dict(self.seed.raw or {}),
        }


@dataclass(frozen=True)
class OpeningSeedParseResult:
    rows: tuple[ParsedOpeningSeedRow, ...]
    row_count: int
    parsed_count: int
    parser_status: str
    parser_missing_fields: tuple[str, ...] = ()


class OpeningBurstScheduler:
    def __init__(
        self,
        gateway_state: GatewayStateStore,
        *,
        config: OpeningBurstRuntimeConfig | None = None,
    ) -> None:
        self.gateway_state = gateway_state
        self.config = config or OpeningBurstRuntimeConfig.from_env()

    def enqueue_if_due(self, now: datetime) -> dict[str, Any]:
        cfg = self.config
        summary = {
            "enabled": cfg.enabled,
            "observe_only": cfg.observe_only,
            "trading_mode": cfg.trading_mode,
            "scheduled": False,
            "enqueued": False,
            "duplicate": False,
            "paused_reason": "",
            "command_id": "",
            "idempotency_key": "",
            "seed_time": "",
            "trade_date": "",
        }
        current = _as_kst(now)
        trade_date = current.date().isoformat()
        seed_time = current.strftime("%H:%M")
        summary["trade_date"] = trade_date
        summary["seed_time"] = seed_time
        if not cfg.enabled:
            summary["paused_reason"] = "DISABLED"
            return summary
        if cfg.observe_only and cfg.trading_mode != "OBSERVE":
            summary["paused_reason"] = "NOT_OBSERVE_MODE"
            return summary
        if not _is_regular_session(current):
            summary["paused_reason"] = "NOT_REGULAR_SESSION"
            return summary
        if seed_time not in set(cfg.seed_times):
            summary["paused_reason"] = "NOT_SEED_TIME"
            return summary
        summary["scheduled"] = True
        command = opening_seed_tr_command(cfg, trade_date=trade_date, seed_time=seed_time)
        summary["command_id"] = command.command_id
        summary["idempotency_key"] = command.idempotency_key
        if self.gateway_state.has_duplicate(command.idempotency_key):
            summary["duplicate"] = True
            summary["paused_reason"] = "DUPLICATE_SEED_TIME"
            return summary
        result = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.NORMAL,
            ttl_sec=cfg.tr_ttl_sec,
            max_attempts=1,
            metadata={"runtime": "opening_theme_burst", "purpose": OPENING_TURNOVER_SEED_PURPOSE},
            now=now,
        )
        summary["enqueued"] = bool(result.accepted)
        if not result.accepted:
            summary["paused_reason"] = str(result.reason or "ENQUEUE_REJECTED")
            if result.reason == "DUPLICATE_COMMAND":
                summary["duplicate"] = True
        return summary


class OpeningThemeBurstRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        gateway_state: GatewayStateStore,
        market_data: MarketDataStore,
        repository: ThemeEngineRepository,
        config: OpeningBurstRuntimeConfig | None = None,
        engine: OpeningThemeBurstEngine | None = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.market_data = market_data
        self.repository = repository
        self.config = config or OpeningBurstRuntimeConfig.from_env()
        self.scheduler = OpeningBurstScheduler(gateway_state, config=self.config)
        self.engine = engine or OpeningThemeBurstEngine(self.config.engine_config())
        self.seed_collector = OpeningTurnoverSeedCollector(self.config.engine_config())
        self.last_summary = empty_opening_theme_burst_section(enabled=self.config.enabled, observe_only=self.config.observe_only)
        self.last_result: OpeningThemeBurstResult | None = None
        self.warnings: list[str] = []

    def run(self, now: datetime) -> dict[str, Any]:
        current = _as_kst(now)
        trade_date = current.date().isoformat()
        summary = empty_opening_theme_burst_section(enabled=self.config.enabled, observe_only=self.config.observe_only)
        summary["trade_date"] = trade_date
        summary["calculated_at"] = current.isoformat()
        scheduler_summary = self.scheduler.enqueue_if_due(current)
        summary["scheduler"] = scheduler_summary
        if scheduler_summary.get("paused_reason") and scheduler_summary.get("paused_reason") not in {"DISABLED", "NOT_SEED_TIME"}:
            summary["warnings"].append(str(scheduler_summary["paused_reason"]))
        hard_pause = str(scheduler_summary.get("paused_reason") or "") in {
            "DISABLED",
            "NOT_OBSERVE_MODE",
            "NOT_REGULAR_SESSION",
        }
        if hard_pause:
            summary["status"] = "DISABLED" if scheduler_summary.get("paused_reason") == "DISABLED" else "SKIPPED"
            summary["warnings"] = _dedupe(summary["warnings"] + self.warnings)
            self.warnings = []
            self.last_summary = summary
            return dict(summary)
        self._hydrate_seed_batches_from_command_history(trade_date)
        seed_batches = self._load_seed_batches(trade_date)
        seed_union = self.seed_collector.collect(seed_batches)
        latest_seed = self._latest_seed_batch(trade_date)
        summary["last_seed_batch_at"] = str((latest_seed or {}).get("batch_time") or "")
        summary["seed_batch_count"] = len(seed_batches)
        summary["seed_symbol_count"] = len({seed.stock_code for seed in seed_union if seed.stock_code})
        summary["parser_status"] = _aggregate_parser_status([batch.get("parser_status") for batch in self._batch_headers(trade_date)])
        self._register_realtime(seed_union, summary, trade_date=trade_date, now=current)
        result = self._run_engine(seed_batches, summary, calculated_at=current.isoformat())
        if result is not None:
            self.last_result = result
            self._save_result(result, summary, trade_date=trade_date, calculated_at=current.isoformat())
            _merge_result_summary(summary, result)
        else:
            latest_result = _safe_latest_opening_result(self.db, trade_date=trade_date)
            if latest_result:
                summary.update(_dashboard_fields_from_result_payload(latest_result))
        summary["warnings"] = _dedupe(summary["warnings"] + self.warnings)
        self.warnings = []
        self.last_summary = summary
        return dict(summary)

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type != "command_ack":
            return False
        payload = dict(event.payload or {})
        if str(payload.get("purpose") or "") != OPENING_TURNOVER_SEED_PURPOSE:
            return False
        self.save_seed_batch_from_ack(payload, event=event)
        return True

    def save_seed_batch_from_ack(self, payload: Mapping[str, Any], *, event: GatewayEvent | None = None) -> dict[str, Any]:
        raw = dict(payload.get("raw") or {})
        rows = [dict(row) for row in list(raw.get("tr_rows") or payload.get("tr_rows") or []) if isinstance(row, Mapping)]
        command_id = str(payload.get("command_id") or (event.command_id if event else "") or "")
        command = self.gateway_state.get_command(command_id) if command_id else None
        command_payload = dict(getattr(command, "command", None).payload or {}) if command is not None else {}
        trade_date = str(payload.get("trade_date") or command_payload.get("trade_date") or _as_kst(datetime.now()).date().isoformat())
        batch_time = str(payload.get("seed_time") or command_payload.get("seed_time") or _seed_time_from_idempotency(payload) or "")
        parsed = parse_opt10032_seed_rows(rows, batch_time=batch_time)
        batch = {
            "trade_date": trade_date,
            "batch_time": batch_time,
            "command_id": command_id,
            "row_count": parsed.row_count,
            "parsed_count": parsed.parsed_count,
            "parser_status": parsed.parser_status,
            "parser_missing_fields": list(parsed.parser_missing_fields),
            "raw": {
                "ack_payload": _redacted_ack_payload(payload),
                "errors": list(raw.get("errors") or []),
                "warnings": list(raw.get("warnings") or []),
            },
            "rows": [row.to_storage_dict() for row in parsed.rows],
        }
        save = getattr(self.db, "save_opening_turnover_seed_batch", None)
        if callable(save):
            return dict(save(batch) or batch)
        return batch

    def drain_warnings(self) -> list[str]:
        warnings = list(self.warnings)
        self.warnings = []
        return warnings

    def _hydrate_seed_batches_from_command_history(self, trade_date: str) -> None:
        for record in self.gateway_state.list_commands(
            status="ACKED",
            include_finished=True,
            command_type="tr_request",
            limit=50,
        ):
            payload = _record_payload(record)
            result_payload = dict(record.get("result_payload") or {})
            if str(payload.get("purpose") or result_payload.get("purpose") or "") != OPENING_TURNOVER_SEED_PURPOSE:
                continue
            if str(payload.get("trade_date") or result_payload.get("trade_date") or trade_date)[:10] != trade_date:
                continue
            if not (dict(result_payload.get("raw") or {}).get("tr_rows") or result_payload.get("tr_rows")):
                continue
            self.save_seed_batch_from_ack({**payload, **result_payload, "command_id": record.get("command_id") or ""})

    def _batch_headers(self, trade_date: str) -> list[dict[str, Any]]:
        loader = getattr(self.db, "list_opening_turnover_seed_batches", None)
        if not callable(loader):
            return []
        return list(loader(trade_date=trade_date, limit=20) or [])

    def _latest_seed_batch(self, trade_date: str) -> dict[str, Any]:
        batches = self._batch_headers(trade_date)
        return batches[-1] if batches else {}

    def _load_seed_batches(self, trade_date: str) -> list[list[OpeningTurnoverSeed]]:
        batch_loader = getattr(self.db, "list_opening_turnover_seed_batches", None)
        row_loader = getattr(self.db, "list_opening_turnover_seed_rows", None)
        if not callable(batch_loader) or not callable(row_loader):
            return []
        batches = list(batch_loader(trade_date=trade_date, limit=len(self.config.seed_times) or 5) or [])
        result: list[list[OpeningTurnoverSeed]] = []
        for batch in batches:
            batch_id = int(batch.get("id") or 0)
            rows = list(row_loader(batch_id=batch_id, limit=self.config.top_n_per_call) or [])
            seeds = [_seed_from_storage_row(row) for row in rows]
            result.append([seed for seed in seeds if seed.stock_code])
        return result

    def _register_realtime(
        self,
        seed_union: Iterable[OpeningTurnoverSeed],
        summary: dict[str, Any],
        *,
        trade_date: str,
        now: datetime,
    ) -> None:
        seeds = list(seed_union)
        if not seeds:
            summary["realtime_registration"] = {"status": "SKIPPED", "reason": "NO_SEEDS"}
            return
        theme_codes = _theme_membership_codes(self.repository)
        selected, excluded = select_realtime_registration_targets(
            seeds,
            theme_codes=theme_codes,
            max_count=self.config.max_realtime_register,
        )
        summary["realtime_excluded_count"] = len(excluded)
        summary["realtime_exclusions"] = excluded[:20]
        if not selected:
            summary["realtime_registration"] = {"status": "SKIPPED", "reason": "NO_ELIGIBLE_TARGETS"}
            return
        codes = [seed.stock_code for seed in selected]
        digest = hashlib.sha256(",".join(codes).encode("utf-8")).hexdigest()[:16]
        idempotency_key = f"opening_burst:register_realtime:{trade_date}:{digest}"
        command = GatewayCommand(
            type="register_realtime",
            command_id=new_message_id("cmd_opening_rt"),
            idempotency_key=idempotency_key,
            source="strategy_runtime",
            payload={
                "purpose": "opening_theme_burst_realtime",
                "screen_no": self.config.realtime_screen_no,
                "codes": codes,
                "code_sources": {code: ["opening_turnover_seed"] + (["theme_membership"] if code in theme_codes else []) for code in codes},
                "code_protected": {code: False for code in codes},
                "trade_date": trade_date,
                "registered_at": now.isoformat(),
                "max_realtime_register": self.config.max_realtime_register,
            },
        )
        enqueue = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.NORMAL,
            ttl_sec=self.config.register_ttl_sec,
            max_attempts=1,
            metadata={"runtime": "opening_theme_burst", "purpose": "opening_theme_burst_realtime"},
            now=now,
        )
        status = "QUEUED" if enqueue.accepted else "REJECTED"
        if not enqueue.accepted and enqueue.reason == "DUPLICATE_COMMAND":
            status = "DUPLICATE"
        summary["realtime_registered_count"] = len(codes) if enqueue.accepted else 0
        summary["realtime_registration"] = {
            "status": status,
            "reason": enqueue.reason or ("QUEUED" if enqueue.accepted else "REJECTED"),
            "duplicate_of": enqueue.duplicate_of,
            "command_id": command.command_id,
            "idempotency_key": idempotency_key,
            "target_count": len(codes),
            "limit": self.config.max_realtime_register,
        }

    def _run_engine(
        self,
        seed_batches: list[list[OpeningTurnoverSeed]],
        summary: dict[str, Any],
        *,
        calculated_at: str,
    ) -> OpeningThemeBurstResult | None:
        if not self.config.enabled:
            summary["status"] = "DISABLED"
            return None
        if self.config.observe_only and self.config.trading_mode != "OBSERVE":
            summary["status"] = "SKIPPED"
            summary["warnings"].append("NOT_OBSERVE_MODE")
            return None
        if not seed_batches:
            summary["status"] = "WAITING_FOR_SEED"
            return None
        theme_inputs = _load_theme_inputs(self.repository)
        if not theme_inputs:
            summary["warnings"].append("OPENING_THEME_MEMBERSHIP_EMPTY")
            return None
        seeds = self.seed_collector.collect(seed_batches)
        snapshots = _snapshots_for_seeds(self.market_data, seeds)
        if not snapshots:
            summary["warnings"].append("OPENING_REALTIME_SNAPSHOT_EMPTY")
            return None
        result = self.engine.run(
            theme_inputs=theme_inputs,
            seed_batches=seed_batches,
            snapshots=snapshots,
            condition_boosts=None,
            calculated_at=calculated_at,
        )
        summary["status"] = "OK"
        return result

    def _save_result(
        self,
        result: OpeningThemeBurstResult,
        summary: dict[str, Any],
        *,
        trade_date: str,
        calculated_at: str,
    ) -> None:
        save = getattr(self.db, "save_opening_theme_burst_result", None)
        if not callable(save):
            return
        payload = opening_result_payload(result)
        payload["runtime_summary"] = dict(summary)
        payload["warnings"] = list(summary.get("warnings") or [])
        save(
            {
                "trade_date": trade_date,
                "calculated_at": calculated_at,
                "output_mode": result.output_mode,
                "ready_allowed": result.ready_allowed,
                "order_intent_allowed": result.order_intent_allowed,
                "seed_batch_count": int(summary.get("seed_batch_count") or 0),
                "seed_symbol_count": int(summary.get("seed_symbol_count") or 0),
                "realtime_registered_count": int(summary.get("realtime_registered_count") or 0),
                "selected_symbols": list(result.selected_symbols),
                "top_themes": payload.get("top_themes") or [],
                "payload": payload,
            }
        )


def opening_seed_tr_command(cfg: OpeningBurstRuntimeConfig, *, trade_date: str, seed_time: str) -> GatewayCommand:
    compact_time = seed_time.replace(":", "")
    idempotency_key = f"opening_burst:seed:{trade_date}:{compact_time}"
    return GatewayCommand(
        type="tr_request",
        command_id=new_message_id("cmd_opening_seed"),
        idempotency_key=idempotency_key,
        source="strategy_runtime",
        payload={
            "purpose": OPENING_TURNOVER_SEED_PURPOSE,
            "response_mode": "capture",
            "tr_code": OPENING_TR_CODE,
            "rq_name": OPENING_RQ_NAME,
            "screen_no": cfg.tr_screen_no,
            "inputs": {},
            "fields": list(OPT10032_FIELDS),
            "trade_date": trade_date,
            "seed_time": seed_time,
            "top_n": cfg.top_n_per_call,
        },
    )


def parse_opt10032_seed_rows(rows: Iterable[Mapping[str, Any]], *, batch_time: str = "") -> OpeningSeedParseResult:
    parsed_rows: list[ParsedOpeningSeedRow] = []
    all_missing: list[str] = []
    raw_rows = [dict(row) for row in rows]
    for index, raw in enumerate(raw_rows, start=1):
        normalized = {_normalize_field_name(key): value for key, value in raw.items()}
        code = normalize_stock_code(str(_field_value(normalized, _CODE_FIELDS) or ""))
        name = str(_field_value(normalized, _NAME_FIELDS) or "").strip()
        rank = _int(_field_value(normalized, _RANK_FIELDS)) or index
        turnover = abs(_float(_field_value(normalized, _TURNOVER_FIELDS)))
        change_rate = _float(_field_value(normalized, _CHANGE_RATE_FIELDS))
        current_price = abs(_float(_field_value(normalized, _PRICE_FIELDS)))
        volume = int(abs(_float(_field_value(normalized, _VOLUME_FIELDS))))
        missing = []
        if not code:
            missing.append("stock_code")
        if not name:
            missing.append("stock_name")
        if turnover <= 0:
            missing.append("turnover_krw")
        status = "PARTIAL" if missing else "OK"
        all_missing.extend(missing)
        parsed_rows.append(
            ParsedOpeningSeedRow(
                seed=OpeningTurnoverSeed(
                    stock_code=code,
                    stock_name=name,
                    turnover_krw=turnover,
                    change_rate_pct=change_rate,
                    seed_rank=rank,
                    first_seen_at=batch_time,
                    last_seen_at=batch_time,
                    seed_times=(batch_time,) if batch_time else (),
                    raw=raw,
                ),
                current_price=current_price,
                volume=volume,
                parser_status=status,
                parser_missing_fields=tuple(missing),
            )
        )
    parsed_count = sum(1 for row in parsed_rows if row.seed.stock_code)
    if not raw_rows:
        parser_status = "EMPTY"
    elif all(row.parser_status == "OK" for row in parsed_rows):
        parser_status = "OK"
    elif parsed_count:
        parser_status = "PARTIAL"
    else:
        parser_status = "MISSING_REQUIRED_FIELDS"
    return OpeningSeedParseResult(
        rows=tuple(parsed_rows),
        row_count=len(raw_rows),
        parsed_count=parsed_count,
        parser_status=parser_status,
        parser_missing_fields=_dedupe(all_missing),
    )


def select_realtime_registration_targets(
    seeds: Iterable[OpeningTurnoverSeed],
    *,
    theme_codes: set[str] | None = None,
    max_count: int,
) -> tuple[list[OpeningTurnoverSeed], list[dict[str, Any]]]:
    theme_codes = set(theme_codes or set())
    selected: list[OpeningTurnoverSeed] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates = sorted(
        [seed for seed in seeds if seed.stock_code],
        key=lambda seed: (
            0 if seed.stock_code in theme_codes else 1,
            _positive_rank(seed.seed_rank),
            -float(seed.turnover_krw or 0.0),
            seed.stock_code,
        ),
    )
    for seed in candidates:
        if seed.stock_code in seen:
            continue
        seen.add(seed.stock_code)
        reason = realtime_exclusion_reason(seed)
        if reason:
            excluded.append({"stock_code": seed.stock_code, "stock_name": seed.stock_name, "reason": reason})
            continue
        if len(selected) >= max(0, int(max_count)):
            excluded.append({"stock_code": seed.stock_code, "stock_name": seed.stock_name, "reason": "REALTIME_REGISTER_LIMIT"})
            continue
        selected.append(seed)
    return selected, excluded


def realtime_exclusion_reason(seed: OpeningTurnoverSeed) -> str:
    raw = dict(seed.raw or {})
    bool_fields = {
        "is_etf": "ETF_EXCLUDED",
        "is_etn": "ETN_EXCLUDED",
        "is_spac": "SPAC_EXCLUDED",
        "is_preferred": "PREFERRED_STOCK_EXCLUDED",
        "is_suspended": "SUSPENDED_EXCLUDED",
        "is_under_administration": "ADMINISTRATION_EXCLUDED",
    }
    for key, reason in bool_fields.items():
        if _truthy(raw.get(key)):
            return reason
    text = " ".join(
        [
            str(seed.stock_name or ""),
            str(raw.get("instrument_type") or ""),
            str(raw.get("security_type") or ""),
            str(raw.get("\uc885\ubaa9\ubd84\ub958") or ""),
        ]
    ).upper()
    if "ETF" in text:
        return "ETF_EXCLUDED"
    if "ETN" in text:
        return "ETN_EXCLUDED"
    if "SPAC" in text or "\uc2a4\ud329" in text:
        return "SPAC_EXCLUDED"
    if "\uac70\ub798\uc815\uc9c0" in text or _truthy(raw.get("\uac70\ub798\uc815\uc9c0")):
        return "SUSPENDED_EXCLUDED"
    if "\uad00\ub9ac" in text or _truthy(raw.get("\uad00\ub9ac\uc885\ubaa9")):
        return "ADMINISTRATION_EXCLUDED"
    if seed.stock_name.endswith("\uc6b0") or "\uc6b0B" in seed.stock_name or "\uc6b0C" in seed.stock_name:
        return "PREFERRED_STOCK_EXCLUDED"
    return ""


def opening_result_payload(result: OpeningThemeBurstResult) -> dict[str, Any]:
    payload = _jsonable(asdict(result))
    ranked = []
    for rank in result.ranked_themes:
        ranked.append(
            {
                "rank": rank.rank,
                "theme_id": rank.theme_id,
                "theme_name": rank.theme_name,
                "theme_score": rank.theme_score,
                "status": _enum_value(rank.status),
                "leader_symbol": rank.leader_symbol,
                "leader_name": rank.leader_name,
                "co_leader_symbols": list(rank.co_leader_symbols),
            }
        )
    selected = [_jsonable(asdict(item)) for item in result.selected]
    excluded = [_jsonable(asdict(item)) for item in result.excluded]
    payload["top_themes"] = ranked
    payload["selected_symbols"] = list(result.selected_symbols)
    payload["selected"] = selected
    payload["excluded"] = excluded
    payload["excluded_late_laggard_count"] = sum(
        1 for item in result.excluded if _enum_value(item.role) == StockLeadershipRole.LATE_LAGGARD.value
    )
    payload["excluded_overheated_count"] = sum(
        1 for item in result.excluded if _enum_value(item.role) == StockLeadershipRole.OVERHEATED.value
    )
    payload["ready_allowed"] = False
    payload["order_intent_allowed"] = False
    payload["output_mode"] = OPENING_BURST_OUTPUT_MODE
    return payload


def opening_theme_burst_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    latest = _safe_latest_opening_result(db, trade_date=trade_date or "")
    batches = []
    loader = getattr(db, "list_opening_turnover_seed_batches", None)
    if callable(loader):
        try:
            batches = list(loader(trade_date=trade_date, limit=20) or [])
        except Exception:
            batches = []
    section = empty_opening_theme_burst_section()
    section["seed_batch_count"] = len(batches)
    section["last_seed_batch_at"] = str((batches[-1] if batches else {}).get("batch_time") or "")
    section["parser_status"] = _aggregate_parser_status([batch.get("parser_status") for batch in batches])
    if latest:
        section.update(_dashboard_fields_from_result_payload(latest))
    return section


def empty_opening_theme_burst_section(*, enabled: bool = False, observe_only: bool = True) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "observe_only": observe_only,
        "status": "DISABLED" if not enabled else "WAITING",
        "trade_date": "",
        "calculated_at": "",
        "last_seed_batch_at": "",
        "seed_batch_count": 0,
        "seed_symbol_count": 0,
        "realtime_registered_count": 0,
        "top_themes": [],
        "selected_symbols": [],
        "excluded_late_laggard_count": 0,
        "excluded_overheated_count": 0,
        "parser_status": "",
        "warnings": [],
        "ready_allowed": False,
        "order_intent_allowed": False,
        "output_mode": OPENING_BURST_OUTPUT_MODE,
    }


_CODE_FIELDS = ("stock_code", "code", "\uc885\ubaa9\ucf54\ub4dc", "\uc885\ubaa9\ubc88\ud638", "\ub2e8\ucd95\ucf54\ub4dc")
_NAME_FIELDS = ("stock_name", "name", "\uc885\ubaa9\uba85")
_PRICE_FIELDS = ("current_price", "price", "\ud604\uc7ac\uac00")
_CHANGE_RATE_FIELDS = ("change_rate_pct", "change_rate", "\ub4f1\ub77d\ub960", "\ub4f1\ub77d\uc728")
_TURNOVER_FIELDS = ("turnover_krw", "turnover", "trade_value", "\uac70\ub798\ub300\uae08")
_VOLUME_FIELDS = ("volume", "cum_volume", "\uac70\ub798\ub7c9")
_RANK_FIELDS = ("rank", "seed_rank", "\uc21c\uc704")


def _load_theme_inputs(repository: ThemeEngineRepository) -> list[tuple[str, str, list[ThemeMembership]]]:
    allowed = {ThemeStatus.ACTIVE.value, ThemeStatus.WATCH.value, ThemeStatus.CANDIDATE.value}
    themes = [theme for theme in repository.list_canonical_themes() if _enum_value(theme.status) in allowed]
    member_loader = getattr(repository, "list_members_by_theme_ids", None)
    if callable(member_loader):
        grouped = member_loader([theme.theme_id for theme in themes], active=True)
        return [
            (theme.theme_id, theme.display_name or theme.canonical_name, list(grouped.get(theme.theme_id) or []))
            for theme in themes
            if grouped.get(theme.theme_id)
        ]
    return [
        (theme.theme_id, theme.display_name or theme.canonical_name, members)
        for theme in themes
        for members in [repository.get_members_by_theme(theme.theme_id, active=True)]
        if members
    ]


def _theme_membership_codes(repository: ThemeEngineRepository) -> set[str]:
    codes: set[str] = set()
    for _, _, members in _load_theme_inputs(repository):
        for member in members:
            code = normalize_code(member.stock_code)
            if code:
                codes.add(code)
    return codes


def _snapshots_for_seeds(market_data: MarketDataStore, seeds: Iterable[OpeningTurnoverSeed]) -> dict[str, StockSnapshot]:
    snapshots: dict[str, StockSnapshot] = {}
    for seed in seeds:
        tick = market_data.latest_tick(seed.stock_code)
        if tick is None:
            continue
        snapshots[seed.stock_code] = _stock_snapshot_from_tick(tick, seed=seed)
    return snapshots


def _stock_snapshot_from_tick(tick: StrategyTick, *, seed: OpeningTurnoverSeed | None = None) -> StockSnapshot:
    metadata = dict(tick.metadata or {})
    return StockSnapshot(
        stock_code=tick.code,
        stock_name=str(metadata.get("name") or metadata.get("stock_name") or (seed.stock_name if seed else "") or ""),
        current_price=float(tick.price or 0),
        change_rate=float(tick.change_rate or (seed.change_rate_pct if seed else 0.0) or 0.0),
        volume=int(tick.cum_volume or 0),
        turnover=float(tick.trade_value or (seed.turnover_krw if seed else 0.0) or 0.0),
        execution_strength=float(tick.execution_strength or 0.0),
        best_bid=float(tick.best_bid or 0),
        best_ask=float(tick.best_ask or 0),
        session_high=float(metadata.get("session_high") or metadata.get("day_high") or tick.price or 0),
        session_low=float(metadata.get("session_low") or metadata.get("day_low") or 0),
        momentum_1m=_float(metadata.get("momentum_1m")),
        momentum_3m=_float(metadata.get("momentum_3m")),
        momentum_5m=_float(metadata.get("momentum_5m")),
        turnover_strength=max(0.0, _float(metadata.get("turnover_strength")) or 1.0),
        ts=tick.timestamp.isoformat() if tick.timestamp else "",
        updated_at=tick.timestamp.isoformat() if tick.timestamp else "",
        metadata=metadata,
    )


def _seed_from_storage_row(row: Mapping[str, Any]) -> OpeningTurnoverSeed:
    raw = dict(row.get("raw") or {})
    batch_time = str(row.get("batch_time") or "")
    return OpeningTurnoverSeed(
        stock_code=normalize_code(row.get("stock_code") or ""),
        stock_name=str(row.get("stock_name") or ""),
        seed_rank=_int(row.get("rank")),
        turnover_krw=_float(row.get("turnover_krw")),
        change_rate_pct=_float(row.get("change_rate_pct")),
        first_seen_at=batch_time,
        last_seen_at=batch_time,
        seed_times=(batch_time,) if batch_time else (),
        raw=raw,
    )


def _merge_result_summary(summary: dict[str, Any], result: OpeningThemeBurstResult) -> None:
    payload = opening_result_payload(result)
    summary.update(_dashboard_fields_from_result_payload(payload))
    summary["ready_allowed"] = False
    summary["order_intent_allowed"] = False
    summary["output_mode"] = OPENING_BURST_OUTPUT_MODE


def _dashboard_fields_from_result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result_payload = dict(payload.get("payload") or payload)
    runtime = dict(result_payload.get("runtime_summary") or {})
    return {
        "status": runtime.get("status") or "OK",
        "trade_date": str(payload.get("trade_date") or runtime.get("trade_date") or ""),
        "calculated_at": str(payload.get("calculated_at") or result_payload.get("calculated_at") or runtime.get("calculated_at") or ""),
        "seed_batch_count": int(payload.get("seed_batch_count") or runtime.get("seed_batch_count") or 0),
        "seed_symbol_count": int(payload.get("seed_symbol_count") or runtime.get("seed_symbol_count") or 0),
        "realtime_registered_count": int(payload.get("realtime_registered_count") or runtime.get("realtime_registered_count") or 0),
        "top_themes": list(payload.get("top_themes") or result_payload.get("top_themes") or [])[:10],
        "selected_symbols": list(payload.get("selected_symbols") or result_payload.get("selected_symbols") or []),
        "excluded_late_laggard_count": int(result_payload.get("excluded_late_laggard_count") or 0),
        "excluded_overheated_count": int(result_payload.get("excluded_overheated_count") or 0),
        "parser_status": runtime.get("parser_status") or "",
        "warnings": list(result_payload.get("warnings") or runtime.get("warnings") or []),
        "ready_allowed": False,
        "order_intent_allowed": False,
        "output_mode": OPENING_BURST_OUTPUT_MODE,
    }


def _safe_latest_opening_result(db: Any, *, trade_date: str = "") -> dict[str, Any]:
    loader = getattr(db, "latest_opening_theme_burst_result", None)
    if not callable(loader):
        return {}
    try:
        return dict(loader(trade_date=trade_date or None) or {})
    except Exception:
        return {}


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    command = dict(record.get("command") or {})
    payload = command.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    payload = record.get("payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _seed_time_from_idempotency(payload: Mapping[str, Any]) -> str:
    key = str(payload.get("idempotency_key") or "")
    tail = key.rsplit(":", 1)[-1]
    if len(tail) == 4 and tail.isdigit():
        return f"{tail[:2]}:{tail[2:]}"
    return ""


def _redacted_ack_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("transport_trace", None)
    raw = dict(result.get("raw") or {})
    if raw.get("tr_rows"):
        raw["tr_rows"] = list(raw.get("tr_rows") or [])
    result["raw"] = raw
    return result


def _aggregate_parser_status(values: Iterable[Any]) -> str:
    statuses = [str(value or "").strip().upper() for value in values if str(value or "").strip()]
    if not statuses:
        return ""
    if all(status == "OK" for status in statuses):
        return "OK"
    if any(status == "MISSING_REQUIRED_FIELDS" for status in statuses):
        return "MISSING_REQUIRED_FIELDS"
    if any(status == "PARTIAL" for status in statuses):
        return "PARTIAL"
    if all(status == "EMPTY" for status in statuses):
        return "EMPTY"
    return statuses[-1]


def _field_value(normalized: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    for alias in aliases:
        key = _normalize_field_name(alias)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_field_name(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _valid_seed_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return ""
    return parsed.strftime("%H:%M")


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(KST).replace(microsecond=0)


def _is_regular_session(value: datetime) -> bool:
    current = _as_kst(value)
    if current.weekday() >= 5:
        return False
    return REGULAR_OPEN <= current.time() <= REGULAR_CLOSE


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "\uc608"}
    return bool(value)


def _float(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).strip().replace(",", "").replace("+", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    if value in {None, ""}:
        return 0
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _positive_rank(value: int) -> int:
    parsed = _int(value)
    return parsed if parsed > 0 else 999999


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


__all__ = [
    "OPENING_BURST_OUTPUT_MODE",
    "OPENING_RQ_NAME",
    "OPENING_TR_CODE",
    "OPENING_TURNOVER_SEED_PURPOSE",
    "OpeningBurstRuntimeConfig",
    "OpeningBurstScheduler",
    "OpeningSeedParseResult",
    "OpeningThemeBurstRuntimePipeline",
    "ParsedOpeningSeedRow",
    "empty_opening_theme_burst_section",
    "opening_result_payload",
    "opening_seed_tr_command",
    "opening_theme_burst_dashboard_section",
    "parse_opt10032_seed_rows",
    "realtime_exclusion_reason",
    "select_realtime_registration_targets",
]
