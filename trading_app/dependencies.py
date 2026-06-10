from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from storage.db import TradingDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"
DEFAULT_LOCAL_TOKEN = "local-dev-token"


@dataclass(frozen=True)
class CoreSettings:
    db_path: Path
    local_token: str
    mode: str = "OBSERVE"
    allow_live: bool = False
    max_order_amount: int = 3_000_000
    max_daily_orders_per_code: int = 5
    command_ttl_sec: int = 30
    command_max_attempts: int = 1
    command_dedupe_retention_sec: int = 86400
    command_history_retention_sec: int = 604800
    command_recovery_expire_stale_dispatched: bool = True
    runtime_enabled: bool = False
    runtime_auto_start: bool = False
    runtime_mode: str = "OBSERVE"
    runtime_evaluation_interval_sec: int = 5
    runtime_cycle_timeout_sec: int = 30
    runtime_allow_dry_run_orders: bool = False
    runtime_allow_live_orders: bool = False
    runtime_require_gateway_heartbeat: bool = True
    runtime_require_kiwoom_login: bool = True
    runtime_require_orderable_for_order: bool = True
    runtime_dry_run_account: str = ""
    runtime_dry_run_position_amount: int = 1_000_000
    runtime_dry_run_min_quantity: int = 1
    runtime_dry_run_hoga: str = "00"
    runtime_dry_run_order_type_buy: int = 1
    runtime_dry_run_order_type_sell: int = 2
    runtime_dry_run_require_account: bool = False
    runtime_dry_run_respect_weight_pct: bool = True
    exit_context_risk_enabled: bool = False
    dry_run_fp_loss_threshold_pct: float = -1.0
    dry_run_fp_drawdown_threshold_pct: float = -3.0
    dry_run_fn_rally_threshold_pct: float = 3.0
    dry_run_good_trade_threshold_pct: float = 2.0
    dry_run_min_hold_minutes_for_final: int = 20
    dry_run_pending_grace_minutes: int = 30
    transport_metrics_enabled: bool = True
    transport_metrics_sample_price_tick_rate: float = 0.01
    transport_metrics_persist_ws_price_ticks: bool = False
    transport_metrics_sample_heartbeat_rate: float = 0.1
    transport_metrics_retention_sec: int = 604800
    transport_latency_p95_warn_ms: int = 1000
    transport_latency_p99_warn_ms: int = 3000
    transport_command_p95_warn_ms: int = 1000
    transport_event_p95_warn_ms: int = 500
    transport_ack_p95_warn_ms: int = 1000
    transport_websocket_recommend_p95_ms: int = 1000
    transport_websocket_recommend_empty_poll_rate: float = 0.8
    transport_websocket_experiment_enabled: bool = False
    threshold_ab_min_sample_count: int = 10
    threshold_ab_min_trade_days: int = 5
    threshold_ab_min_completed_lifecycles: int = 30
    threshold_ab_min_entry_intents: int = 30
    threshold_ab_min_exit_decisions: int = 10
    threshold_ab_min_signal_samples: int = 5
    threshold_ab_strong_fp_reduction_min: int = 3
    threshold_ab_max_fn_increase: int = 1
    threshold_ab_max_opportunity_loss_increase: int = 1
    threshold_ab_confidence_min: float = 0.5
    threshold_ab_export_root: Path = PROJECT_ROOT / "reports" / "dry_run_threshold_ab"
    threshold_ab_enable_apply: bool = False
    intraday_outcome_enabled: bool = True
    intraday_outcome_horizons_sec: str = "60,180,300,600,1200"
    intraday_outcome_min_price_samples: int = 2
    intraday_outcome_tp_threshold_pct: float = 2.0
    intraday_outcome_fn_threshold_pct: float = 2.5
    intraday_outcome_fp_drawdown_pct: float = -1.5
    intraday_outcome_fp_return_pct: float = -1.0
    intraday_outcome_exit_giveback_pct: float = -2.0
    intraday_outcome_max_batch_size: int = 500
    shadow_strategy_enabled: bool = True
    shadow_strategy_policies: str = (
        "relaxed_risk_off_leader,strict_late_chase,strict_entry_risk,"
        "relaxed_data_wait_for_leader,fast_theme_exit_shadow"
    )
    shadow_strategy_max_batch_size: int = 500
    shadow_strategy_runtime_hook_enabled: bool = True
    shadow_strategy_rebuild_limit: int = 10000
    shadow_strategy_min_theme_score: float = 70.0
    shadow_strategy_min_hybrid_score: float = 65.0
    shadow_strategy_ready_small_multiplier: float = 0.3
    shadow_strategy_observe_only: bool = True
    shadow_strategy_allow_apply: bool = False
    change_proposal_enabled: bool = True
    change_proposal_min_sample_count: int = 20
    change_proposal_min_trade_days: int = 2
    change_proposal_min_replay_count: int = 1
    change_proposal_max_fp_increase: int = 1
    change_proposal_max_opportunity_loss_increase: int = 1
    change_proposal_strong_min_confidence: float = 0.7
    change_proposal_allow_auto_apply: bool = False
    change_proposal_default_expire_days: int = 5
    replay_tick_history_enabled: bool = True
    replay_tick_history_queue_max_size: int = 5000
    replay_tick_history_batch_size: int = 200
    replay_tick_history_flush_interval_sec: float = 1.0
    replay_tick_history_min_interval_ms: float = 500.0

    @property
    def live_order_enabled(self) -> bool:
        return self.mode == "LIVE" and self.allow_live


def get_settings() -> CoreSettings:
    mode = os.environ.get("TRADING_MODE", "OBSERVE").strip().upper() or "OBSERVE"
    if mode not in {"OBSERVE", "DRY_RUN", "LIVE"}:
        mode = "OBSERVE"
    runtime_mode = os.environ.get("TRADING_RUNTIME_MODE", "OBSERVE").strip().upper() or "OBSERVE"
    if runtime_mode not in {"OBSERVE", "DRY_RUN"}:
        runtime_mode = "OBSERVE"
    return CoreSettings(
        db_path=Path(os.environ.get("TRADING_DB_PATH", str(DEFAULT_DB_PATH))).expanduser(),
        local_token=os.environ.get("TRADING_CORE_TOKEN", DEFAULT_LOCAL_TOKEN),
        mode=mode,
        allow_live=os.environ.get("TRADING_ALLOW_LIVE", "0") == "1",
        max_order_amount=_int_env("TRADING_MAX_ORDER_AMOUNT", 3_000_000),
        max_daily_orders_per_code=_int_env("TRADING_MAX_DAILY_ORDERS_PER_CODE", 5),
        command_ttl_sec=_int_env("TRADING_ORDER_COMMAND_TTL_SEC", 30),
        command_max_attempts=_int_env("TRADING_ORDER_COMMAND_MAX_ATTEMPTS", 1),
        command_dedupe_retention_sec=_int_env("TRADING_COMMAND_DEDUPE_RETENTION_SEC", 86400),
        command_history_retention_sec=_int_env("TRADING_COMMAND_HISTORY_RETENTION_SEC", 604800),
        command_recovery_expire_stale_dispatched=os.environ.get(
            "TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED", "1"
        )
        != "0",
        runtime_enabled=_bool_env("TRADING_RUNTIME_ENABLED", False),
        runtime_auto_start=_bool_env("TRADING_RUNTIME_AUTO_START", False),
        runtime_mode=runtime_mode,
        runtime_evaluation_interval_sec=_int_env("TRADING_RUNTIME_EVALUATION_INTERVAL_SEC", 5),
        runtime_cycle_timeout_sec=_int_env("TRADING_RUNTIME_CYCLE_TIMEOUT_SEC", 30),
        runtime_allow_dry_run_orders=_bool_env("TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS", False),
        runtime_allow_live_orders=_bool_env("TRADING_RUNTIME_ALLOW_LIVE_ORDERS", False),
        runtime_require_gateway_heartbeat=_bool_env("TRADING_RUNTIME_REQUIRE_GATEWAY_HEARTBEAT", True),
        runtime_require_kiwoom_login=_bool_env("TRADING_RUNTIME_REQUIRE_KIWOOM_LOGIN", True),
        runtime_require_orderable_for_order=_bool_env("TRADING_RUNTIME_REQUIRE_ORDERABLE_FOR_ORDER", True),
        runtime_dry_run_account=os.environ.get("TRADING_RUNTIME_DRY_RUN_ACCOUNT", ""),
        runtime_dry_run_position_amount=_int_env("TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT", 1_000_000),
        runtime_dry_run_min_quantity=_int_env("TRADING_RUNTIME_DRY_RUN_MIN_QUANTITY", 1),
        runtime_dry_run_hoga=os.environ.get("TRADING_RUNTIME_DRY_RUN_HOGA", "00"),
        runtime_dry_run_order_type_buy=_int_env("TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_BUY", 1),
        runtime_dry_run_order_type_sell=_int_env("TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_SELL", 2),
        runtime_dry_run_require_account=_bool_env("TRADING_RUNTIME_DRY_RUN_REQUIRE_ACCOUNT", False),
        runtime_dry_run_respect_weight_pct=_bool_env("TRADING_RUNTIME_DRY_RUN_RESPECT_WEIGHT_PCT", True),
        exit_context_risk_enabled=_bool_env("TRADING_EXIT_CONTEXT_RISK_ENABLED", False),
        dry_run_fp_loss_threshold_pct=_float_env("TRADING_DRY_RUN_FP_LOSS_THRESHOLD_PCT", -1.0),
        dry_run_fp_drawdown_threshold_pct=_float_env("TRADING_DRY_RUN_FP_DRAWDOWN_THRESHOLD_PCT", -3.0),
        dry_run_fn_rally_threshold_pct=_float_env("TRADING_DRY_RUN_FN_RALLY_THRESHOLD_PCT", 3.0),
        dry_run_good_trade_threshold_pct=_float_env("TRADING_DRY_RUN_GOOD_TRADE_THRESHOLD_PCT", 2.0),
        dry_run_min_hold_minutes_for_final=_int_env("TRADING_DRY_RUN_MIN_HOLD_MINUTES_FOR_FINAL", 20),
        dry_run_pending_grace_minutes=_int_env("TRADING_DRY_RUN_PENDING_GRACE_MINUTES", 30),
        transport_metrics_enabled=_bool_env("TRADING_TRANSPORT_METRICS_ENABLED", True),
        transport_metrics_sample_price_tick_rate=_float_env("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", 0.01),
        transport_metrics_persist_ws_price_ticks=_bool_env("TRADING_TRANSPORT_METRICS_PERSIST_WS_PRICE_TICKS", False),
        transport_metrics_sample_heartbeat_rate=_float_env("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", 0.1),
        transport_metrics_retention_sec=_int_env("TRADING_TRANSPORT_METRICS_RETENTION_SEC", 604800),
        transport_latency_p95_warn_ms=_int_env("TRADING_TRANSPORT_LATENCY_P95_WARN_MS", 1000),
        transport_latency_p99_warn_ms=_int_env("TRADING_TRANSPORT_LATENCY_P99_WARN_MS", 3000),
        transport_command_p95_warn_ms=_int_env("TRADING_TRANSPORT_COMMAND_P95_WARN_MS", 1000),
        transport_event_p95_warn_ms=_int_env("TRADING_TRANSPORT_EVENT_P95_WARN_MS", 500),
        transport_ack_p95_warn_ms=_int_env("TRADING_TRANSPORT_ACK_P95_WARN_MS", 1000),
        transport_websocket_recommend_p95_ms=_int_env("TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_P95_MS", 1000),
        transport_websocket_recommend_empty_poll_rate=_float_env(
            "TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_EMPTY_POLL_RATE",
            0.8,
        ),
        transport_websocket_experiment_enabled=_bool_env("TRADING_TRANSPORT_WEBSOCKET_EXPERIMENT_ENABLED", False),
        threshold_ab_min_sample_count=_int_env("TRADING_THRESHOLD_AB_MIN_SAMPLE_COUNT", 10),
        threshold_ab_min_trade_days=_int_env("TRADING_THRESHOLD_AB_MIN_TRADE_DAYS", 5),
        threshold_ab_min_completed_lifecycles=_int_env("TRADING_THRESHOLD_AB_MIN_COMPLETED_LIFECYCLES", 30),
        threshold_ab_min_entry_intents=_int_env("TRADING_THRESHOLD_AB_MIN_ENTRY_INTENTS", 30),
        threshold_ab_min_exit_decisions=_int_env("TRADING_THRESHOLD_AB_MIN_EXIT_DECISIONS", 10),
        threshold_ab_min_signal_samples=_int_env("TRADING_THRESHOLD_AB_MIN_SIGNAL_SAMPLES", 5),
        threshold_ab_strong_fp_reduction_min=_int_env("TRADING_THRESHOLD_AB_STRONG_FP_REDUCTION_MIN", 3),
        threshold_ab_max_fn_increase=_int_env("TRADING_THRESHOLD_AB_MAX_FN_INCREASE", 1),
        threshold_ab_max_opportunity_loss_increase=_int_env("TRADING_THRESHOLD_AB_MAX_OPPORTUNITY_LOSS_INCREASE", 1),
        threshold_ab_confidence_min=_float_env("TRADING_THRESHOLD_AB_CONFIDENCE_MIN", 0.5),
        threshold_ab_export_root=Path(
            os.environ.get("TRADING_THRESHOLD_AB_EXPORT_ROOT", str(PROJECT_ROOT / "reports" / "dry_run_threshold_ab"))
        ).expanduser(),
        threshold_ab_enable_apply=_bool_env("TRADING_THRESHOLD_AB_ENABLE_APPLY", False),
        intraday_outcome_enabled=_bool_env("TRADING_INTRADAY_OUTCOME_ENABLED", True),
        intraday_outcome_horizons_sec=os.environ.get("TRADING_INTRADAY_OUTCOME_HORIZONS_SEC", "60,180,300,600,1200"),
        intraday_outcome_min_price_samples=_int_env("TRADING_INTRADAY_OUTCOME_MIN_PRICE_SAMPLES", 2),
        intraday_outcome_tp_threshold_pct=_float_env("TRADING_INTRADAY_OUTCOME_TP_THRESHOLD_PCT", 2.0),
        intraday_outcome_fn_threshold_pct=_float_env("TRADING_INTRADAY_OUTCOME_FN_THRESHOLD_PCT", 2.5),
        intraday_outcome_fp_drawdown_pct=_float_env("TRADING_INTRADAY_OUTCOME_FP_DRAWDOWN_PCT", -1.5),
        intraday_outcome_fp_return_pct=_float_env("TRADING_INTRADAY_OUTCOME_FP_RETURN_PCT", -1.0),
        intraday_outcome_exit_giveback_pct=_float_env("TRADING_INTRADAY_OUTCOME_EXIT_GIVEBACK_PCT", -2.0),
        intraday_outcome_max_batch_size=_int_env("TRADING_INTRADAY_OUTCOME_MAX_BATCH_SIZE", 500),
        shadow_strategy_enabled=_bool_env("TRADING_SHADOW_STRATEGY_ENABLED", True),
        shadow_strategy_policies=os.environ.get(
            "TRADING_SHADOW_STRATEGY_POLICIES",
            "relaxed_risk_off_leader,strict_late_chase,strict_entry_risk,relaxed_data_wait_for_leader,fast_theme_exit_shadow",
        ),
        shadow_strategy_max_batch_size=_int_env("TRADING_SHADOW_STRATEGY_MAX_BATCH_SIZE", 500),
        shadow_strategy_runtime_hook_enabled=_bool_env("TRADING_SHADOW_STRATEGY_RUNTIME_HOOK_ENABLED", True),
        shadow_strategy_rebuild_limit=_int_env("TRADING_SHADOW_STRATEGY_REBUILD_LIMIT", 10000),
        shadow_strategy_min_theme_score=_float_env("TRADING_SHADOW_STRATEGY_MIN_THEME_SCORE", 70.0),
        shadow_strategy_min_hybrid_score=_float_env("TRADING_SHADOW_STRATEGY_MIN_HYBRID_SCORE", 65.0),
        shadow_strategy_ready_small_multiplier=_float_env("TRADING_SHADOW_STRATEGY_READY_SMALL_MULTIPLIER", 0.3),
        shadow_strategy_observe_only=_bool_env("TRADING_SHADOW_STRATEGY_OBSERVE_ONLY", True),
        shadow_strategy_allow_apply=_bool_env("TRADING_SHADOW_STRATEGY_ALLOW_APPLY", False),
        change_proposal_enabled=_bool_env("TRADING_CHANGE_PROPOSAL_ENABLED", True),
        change_proposal_min_sample_count=_int_env("TRADING_CHANGE_PROPOSAL_MIN_SAMPLE_COUNT", 20),
        change_proposal_min_trade_days=_int_env("TRADING_CHANGE_PROPOSAL_MIN_TRADE_DAYS", 2),
        change_proposal_min_replay_count=_int_env("TRADING_CHANGE_PROPOSAL_MIN_REPLAY_COUNT", 1),
        change_proposal_max_fp_increase=_int_env("TRADING_CHANGE_PROPOSAL_MAX_FP_INCREASE", 1),
        change_proposal_max_opportunity_loss_increase=_int_env(
            "TRADING_CHANGE_PROPOSAL_MAX_OPPORTUNITY_LOSS_INCREASE",
            1,
        ),
        change_proposal_strong_min_confidence=_float_env("TRADING_CHANGE_PROPOSAL_STRONG_MIN_CONFIDENCE", 0.7),
        change_proposal_allow_auto_apply=_bool_env("TRADING_CHANGE_PROPOSAL_ALLOW_AUTO_APPLY", False),
        change_proposal_default_expire_days=_int_env("TRADING_CHANGE_PROPOSAL_DEFAULT_EXPIRE_DAYS", 5),
        replay_tick_history_enabled=_bool_env("TRADING_REPLAY_TICK_HISTORY_ENABLED", True),
        replay_tick_history_queue_max_size=_int_env("TRADING_REPLAY_TICK_HISTORY_QUEUE_MAX_SIZE", 5000),
        replay_tick_history_batch_size=_int_env("TRADING_REPLAY_TICK_HISTORY_BATCH_SIZE", 200),
        replay_tick_history_flush_interval_sec=_float_env("TRADING_REPLAY_TICK_HISTORY_FLUSH_INTERVAL_SEC", 1.0),
        replay_tick_history_min_interval_ms=_float_env("TRADING_REPLAY_TICK_HISTORY_MIN_INTERVAL_MS", 500.0),
    )


def open_database() -> TradingDatabase:
    return TradingDatabase(str(get_settings().db_path))


def close_database(db: TradingDatabase) -> None:
    try:
        db.close()
    except Exception:
        pass


def extract_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_local_token:
        return x_local_token.strip()
    query_token = request.query_params.get("token")
    return str(query_token or "").strip()


def verify_gateway_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> None:
    expected = get_settings().local_token
    provided = extract_token(request, authorization=authorization, x_local_token=x_local_token)
    if not expected or provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid local gateway token",
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
