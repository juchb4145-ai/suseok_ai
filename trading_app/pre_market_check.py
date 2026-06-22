from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class PreMarketCheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"
    UNKNOWN = "UNKNOWN"


class PreMarketGoNoGoDecision(str, Enum):
    GO_OBSERVE = "GO_OBSERVE"
    GO_DRY_RUN = "GO_DRY_RUN"
    GO_LIVE_SIM_LIMITED = "GO_LIVE_SIM_LIMITED"
    NO_GO = "NO_GO"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True)
class PreMarketCheckConfig:
    requested_mode: str = "OBSERVE"
    strategy_reboot_v2_enabled: bool = True
    dashboard_v2_enabled: bool = True
    dry_run_entry_intents_enabled: bool = False
    dry_run_exit_sell_intents_enabled: bool = False
    order_manager_enabled: bool = False
    order_manager_mode: str = "OBSERVE"
    allow_live_sim_orders: bool = False
    require_simulation_broker: bool = True
    block_real_broker: bool = True
    account_whitelist: tuple[str, ...] = ()
    max_order_quantity: int = 1
    max_order_amount: int = 100_000
    max_daily_buy_orders: int = 3
    max_daily_sell_orders: int = 10
    max_open_positions: int = 3
    cancel_unfilled_after_sec: int = 45
    live_sim_max_order_quantity_ceiling: int = 1
    live_sim_max_order_amount_ceiling: int = 100_000
    opening_burst_configured: bool = False

    @classmethod
    def from_env(cls, requested_mode: str | None = None) -> "PreMarketCheckConfig":
        whitelist = tuple(item.strip() for item in os.getenv("TRADING_LIVE_SIM_ACCOUNT_WHITELIST", "").split(",") if item.strip())
        return cls(
            requested_mode=normalize_requested_mode(requested_mode or os.getenv("TRADING_PRE_MARKET_CHECK_MODE", "OBSERVE")),
            strategy_reboot_v2_enabled=_env_bool("STRATEGY_REBOOT_V2_ENABLED", True),
            dashboard_v2_enabled=_env_bool(
                "TRADING_DASHBOARD_V2_ENABLED",
                _env_bool("STRATEGY_REBOOT_V2_DASHBOARD", True),
            ),
            dry_run_entry_intents_enabled=_env_bool("TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS", False),
            dry_run_exit_sell_intents_enabled=_env_bool("TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS", False),
            order_manager_enabled=_env_bool("TRADING_ORDER_MANAGER_ENABLED", False),
            order_manager_mode=_env_choice("TRADING_ORDER_MANAGER_MODE", "OBSERVE", {"OBSERVE", "DRY_RUN", "LIVE_SIM"}),
            allow_live_sim_orders=_env_bool("TRADING_ALLOW_LIVE_SIM_ORDERS", False),
            require_simulation_broker=_env_bool("TRADING_REQUIRE_SIMULATION_BROKER", True),
            block_real_broker=_env_bool("TRADING_BLOCK_REAL_BROKER", True),
            account_whitelist=whitelist,
            max_order_quantity=_env_int("TRADING_LIVE_SIM_MAX_ORDER_QUANTITY", 1),
            max_order_amount=_env_int("TRADING_LIVE_SIM_MAX_ORDER_AMOUNT", 100_000),
            max_daily_buy_orders=_env_int("TRADING_LIVE_SIM_MAX_DAILY_BUY_ORDERS", 3),
            max_daily_sell_orders=_env_int("TRADING_LIVE_SIM_MAX_DAILY_SELL_ORDERS", 10),
            max_open_positions=_env_int("TRADING_LIVE_SIM_MAX_OPEN_POSITIONS", 3),
            cancel_unfilled_after_sec=_env_int("TRADING_LIVE_SIM_CANCEL_UNFILLED_AFTER_SEC", 45),
            live_sim_max_order_quantity_ceiling=_env_int("TRADING_PRE_MARKET_LIVE_SIM_MAX_QTY_CEILING", 1),
            live_sim_max_order_amount_ceiling=_env_int("TRADING_PRE_MARKET_LIVE_SIM_MAX_AMOUNT_CEILING", 100_000),
            opening_burst_configured=_env_bool(
                "TRADING_OPENING_BURST_CONFIGURED",
                _env_bool("TRADING_OPENING_BURST_ENABLED", False) or _env_bool("TRADING_OPENING_BURST_FALLBACK_CONFIGURED", False),
            ),
        )


@dataclass(frozen=True)
class PreMarketCheckItem:
    key: str
    category: str
    label_ko: str
    status: str
    required: bool = True
    reason_code: str = ""
    message_ko: str = ""
    blocking: bool = False
    manual_review: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class PreMarketCheckReport:
    trade_date: str
    checked_at: str
    requested_mode: str
    go_no_go: str
    summary_status: str
    pass_count: int
    warn_count: int
    fail_count: int
    unknown_count: int
    skip_count: int
    items: tuple[PreMarketCheckItem, ...]
    blocking_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    operator_message_ko: str = ""
    recommended_action_ko: str = ""
    schema_version: str = "pre_market_check.v1"

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


def build_pre_market_check_report(
    snapshot: dict[str, Any] | None,
    *,
    config: PreMarketCheckConfig | None = None,
    now: datetime | None = None,
) -> PreMarketCheckReport:
    base = dict(snapshot or {})
    cfg = config or PreMarketCheckConfig.from_env()
    requested_mode = normalize_requested_mode(cfg.requested_mode)
    current = _clean_time(now)
    trade_date = str(base.get("trade_date") or current.date().isoformat())

    core = dict(base.get("core") or {})
    runtime = dict(base.get("runtime") or {})
    gateway = dict(base.get("gateway") or runtime.get("gateway") or {})
    commands = dict(base.get("commands") or runtime.get("commands") or {})
    system_health = dict(base.get("system_health") or {})
    order_manager = dict(base.get("order_manager") or runtime.get("order_manager") or {})
    market_regime = dict(base.get("market_regime") or runtime.get("market_regime") or {})
    theme_board = dict(base.get("theme_board") or runtime.get("theme_board") or {})
    data_preload = dict(base.get("data_preload") or runtime.get("data_preload") or {})
    sqlite = dict(base.get("sqlite") or {})
    risk = dict(base.get("risk") or runtime.get("risk") or {})

    items: list[PreMarketCheckItem] = []

    def add(
        key: str,
        category: str,
        label_ko: str,
        status: PreMarketCheckStatus | str,
        *,
        reason_code: str = "",
        message_ko: str = "",
        required: bool = True,
        manual_review: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        status_text = _status_value(status)
        items.append(
            PreMarketCheckItem(
                key=key,
                category=category,
                label_ko=label_ko,
                status=status_text,
                required=required,
                reason_code=reason_code,
                message_ko=message_ko,
                blocking=bool(required and status_text == PreMarketCheckStatus.FAIL.value),
                manual_review=bool(manual_review),
                details=dict(details or {}),
            )
        )

    dashboard_available = bool(base.get("dashboard_v2_available") or dict(base.get("dashboard_v2") or {}).get("schema_version"))
    broker_env = _broker_env(gateway, order_manager)
    account = str(order_manager.get("account") or gateway.get("account") or "")
    account_whitelisted = _account_whitelisted(account, cfg, order_manager)
    heartbeat_ok = bool(gateway.get("heartbeat_ok"))
    kiwoom_logged_in = bool(gateway.get("kiwoom_logged_in"))
    orderable = bool(gateway.get("orderable"))
    command_unhealthy = bool(commands.get("unhealthy")) or int(commands.get("queued_count") or 0) >= 1000
    stale_dispatched = int(commands.get("stale_dispatched_count") or commands.get("dispatched_count") or 0)
    kill_switch_state = str(order_manager.get("kill_switch_state") or "NORMAL")
    unmanaged_pending = int(
        order_manager.get("unmanaged_pending_order_count")
        or order_manager.get("open_unmanaged_order_count")
        or base.get("unmanaged_pending_order_count")
        or 0
    )
    market_status = str(market_regime.get("global_status") or market_regime.get("status") or "")

    core_ok = bool(core) or base.get("core_health_ok") is True
    add(
        "core_health",
        "core",
        "Core health",
        PreMarketCheckStatus.PASS if core_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if core_ok else "CORE_HEALTH_MISSING",
        message_ko="Core API 응답 확인",
    )
    add(
        "runtime_status",
        "core",
        "Runtime status",
        PreMarketCheckStatus.FAIL if runtime.get("last_error") else PreMarketCheckStatus.PASS,
        reason_code="RUNTIME_LAST_ERROR" if runtime.get("last_error") else "",
        message_ko=str(runtime.get("last_error") or "Runtime 오류 없음"),
    )
    add(
        "dashboard_v2_snapshot",
        "core",
        "Dashboard V2 snapshot",
        PreMarketCheckStatus.PASS if dashboard_available else PreMarketCheckStatus.WARN,
        reason_code="" if dashboard_available else "DASHBOARD_V2_SNAPSHOT_MISSING",
        message_ko="Dashboard V2 snapshot 확인" if dashboard_available else "Dashboard V2 snapshot이 아직 생성되지 않았습니다.",
        manual_review=not dashboard_available and requested_mode != "OBSERVE",
    )
    sqlite_ok = bool(sqlite) and sqlite.get("writable") is True
    add(
        "sqlite_operational_store",
        "core",
        "SQLite operational store",
        PreMarketCheckStatus.PASS if sqlite_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if sqlite_ok else "SQLITE_OPERATIONAL_STORE_UNHEALTHY",
        message_ko="SQLite operational store 사용 가능" if sqlite_ok else "SQLite operational store 장애는 No-Go입니다.",
        details=sqlite,
    )
    add(
        "latest_migration",
        "core",
        "Latest migration",
        PreMarketCheckStatus.PASS if sqlite_ok else PreMarketCheckStatus.UNKNOWN,
        reason_code="" if sqlite_ok else "MIGRATION_STATUS_UNKNOWN",
        message_ko="DB schema 확인" if sqlite_ok else "DB schema 확인 불가",
    )

    add(
        "gateway_heartbeat",
        "gateway",
        "Gateway heartbeat",
        PreMarketCheckStatus.PASS if heartbeat_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if heartbeat_ok else "GATEWAY_HEARTBEAT_STALE",
        message_ko="Gateway heartbeat fresh" if heartbeat_ok else "Gateway heartbeat 지연 또는 미수신",
        details={"heartbeat_age_sec": gateway.get("heartbeat_age_sec"), "last_heartbeat_at": gateway.get("last_heartbeat_at")},
    )
    add(
        "kiwoom_login",
        "gateway",
        "Kiwoom login",
        PreMarketCheckStatus.PASS if kiwoom_logged_in else PreMarketCheckStatus.FAIL,
        reason_code="" if kiwoom_logged_in else "KIWOOM_LOGIN_FAIL",
        message_ko="Kiwoom 로그인 확인" if kiwoom_logged_in else "Kiwoom 미로그인",
    )
    add(
        "orderable",
        "gateway",
        "Orderable",
        PreMarketCheckStatus.PASS if orderable else (PreMarketCheckStatus.WARN if requested_mode != "LIVE_SIM_LIMITED" else PreMarketCheckStatus.FAIL),
        reason_code="" if orderable else "KIWOOM_NOT_ORDERABLE",
        message_ko="주문 가능 상태 확인" if orderable else "Kiwoom orderable=false",
        manual_review=not orderable and requested_mode != "OBSERVE",
    )
    if broker_env == "REAL":
        broker_status = PreMarketCheckStatus.FAIL
        broker_reason = "REAL_BROKER_DETECTED"
    elif broker_env == "UNKNOWN" and requested_mode == "LIVE_SIM_LIMITED":
        broker_status = PreMarketCheckStatus.FAIL
        broker_reason = "BROKER_ENV_UNKNOWN_FOR_LIVE_SIM"
    elif broker_env == "UNKNOWN":
        broker_status = PreMarketCheckStatus.WARN
        broker_reason = "BROKER_ENV_UNKNOWN"
    else:
        broker_status = PreMarketCheckStatus.PASS
        broker_reason = ""
    add(
        "broker_environment",
        "gateway",
        "Broker environment",
        broker_status,
        reason_code=broker_reason,
        message_ko=f"broker_env={broker_env}",
        manual_review=broker_status == PreMarketCheckStatus.WARN,
        details={"broker_env": broker_env, "account_mode": gateway.get("account_mode") or order_manager.get("account_mode")},
    )
    add(
        "account_present",
        "gateway",
        "Account present",
        PreMarketCheckStatus.PASS if account else (PreMarketCheckStatus.WARN if requested_mode != "LIVE_SIM_LIMITED" else PreMarketCheckStatus.FAIL),
        reason_code="" if account else "ACCOUNT_MISSING",
        message_ko="계좌 확인" if account else "계좌 정보 없음",
        manual_review=not account and requested_mode != "OBSERVE",
    )
    add(
        "account_whitelist",
        "gateway",
        "LIVE_SIM account whitelist",
        PreMarketCheckStatus.PASS if requested_mode != "LIVE_SIM_LIMITED" or account_whitelisted else PreMarketCheckStatus.FAIL,
        reason_code="" if requested_mode != "LIVE_SIM_LIMITED" or account_whitelisted else "ACCOUNT_WHITELIST_FAIL",
        message_ko="계좌 whitelist 통과" if account_whitelisted else "LIVE_SIM 계좌 whitelist 확인 필요",
        required=requested_mode == "LIVE_SIM_LIMITED",
        details={"account": account, "account_whitelisted": account_whitelisted},
    )

    add(
        "command_queue_health",
        "order",
        "Command queue healthy",
        PreMarketCheckStatus.FAIL if command_unhealthy else PreMarketCheckStatus.PASS,
        reason_code="COMMAND_QUEUE_UNHEALTHY" if command_unhealthy else "",
        message_ko="Command queue 정상" if not command_unhealthy else "Command queue 비정상",
        details=commands,
    )
    add(
        "stale_dispatched_commands",
        "order",
        "Stale dispatched command",
        PreMarketCheckStatus.WARN if stale_dispatched else PreMarketCheckStatus.PASS,
        reason_code="STALE_DISPATCHED_COMMANDS" if stale_dispatched else "",
        message_ko="stale dispatched command 없음" if not stale_dispatched else "stale dispatched command 정리 필요",
        manual_review=bool(stale_dispatched),
        details={"stale_dispatched_count": stale_dispatched},
    )
    add(
        "pending_reconcile",
        "order",
        "Pending order reconcile",
        PreMarketCheckStatus.FAIL if unmanaged_pending else PreMarketCheckStatus.PASS,
        reason_code="UNMANAGED_PENDING_ORDER_RECONCILE_REQUIRED" if unmanaged_pending else "",
        message_ko="미관리 미체결 없음" if not unmanaged_pending else "미관리 미체결 reconcile 필요",
        details={"unmanaged_pending_order_count": unmanaged_pending},
    )
    add(
        "cancel_scheduler",
        "order",
        "LIVE_SIM cancel scheduler",
        PreMarketCheckStatus.PASS if requested_mode != "LIVE_SIM_LIMITED" or cfg.cancel_unfilled_after_sec > 0 else PreMarketCheckStatus.FAIL,
        reason_code="" if requested_mode != "LIVE_SIM_LIMITED" or cfg.cancel_unfilled_after_sec > 0 else "CANCEL_SCHEDULER_DISABLED_FOR_LIVE_SIM",
        message_ko="미체결 자동 취소 설정 확인",
        required=requested_mode == "LIVE_SIM_LIMITED",
        details={"cancel_unfilled_after_sec": cfg.cancel_unfilled_after_sec},
    )
    add(
        "kill_switch",
        "order",
        "Kill switch",
        PreMarketCheckStatus.PASS if kill_switch_state in {"", "NORMAL"} else PreMarketCheckStatus.FAIL,
        reason_code="" if kill_switch_state in {"", "NORMAL"} else "KILL_SWITCH_ACTIVE",
        message_ko=f"kill_switch={kill_switch_state or 'NORMAL'}",
    )
    add(
        "order_manager_mode",
        "order",
        "OrderManager mode",
        _order_manager_mode_status(requested_mode, cfg, order_manager),
        reason_code=_order_manager_mode_reason(requested_mode, cfg, order_manager),
        message_ko=f"requested={requested_mode}, order_manager={cfg.order_manager_mode}",
        manual_review=requested_mode == "OBSERVE" and cfg.order_manager_mode == "LIVE_SIM",
        details={"enabled": cfg.order_manager_enabled, "mode": cfg.order_manager_mode, "allow_live_sim_orders": cfg.allow_live_sim_orders},
    )

    _add_reboot_v2_items(add, requested_mode, cfg, runtime, gateway, commands, sqlite_ok, market_regime)
    _add_data_preload_items(add, requested_mode, data_preload, theme_board, market_regime, cfg)
    _add_risk_items(add, requested_mode, cfg, risk, market_status)

    status_counts = {status.value: 0 for status in PreMarketCheckStatus}
    for item in items:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
    blocking_reasons = tuple(item.reason_code for item in items if item.blocking and item.reason_code)
    warning_reasons = tuple(item.reason_code for item in items if item.status in {"WARN", "UNKNOWN"} and item.reason_code)
    decision = _go_no_go_decision(requested_mode, items)
    summary_status = _summary_status(decision)
    return PreMarketCheckReport(
        trade_date=trade_date,
        checked_at=current.isoformat(),
        requested_mode=requested_mode,
        go_no_go=decision.value,
        summary_status=summary_status,
        pass_count=status_counts.get("PASS", 0),
        warn_count=status_counts.get("WARN", 0),
        fail_count=status_counts.get("FAIL", 0),
        unknown_count=status_counts.get("UNKNOWN", 0),
        skip_count=status_counts.get("SKIP", 0),
        items=tuple(items),
        blocking_reasons=blocking_reasons,
        warning_reasons=warning_reasons,
        operator_message_ko=_operator_message(decision),
        recommended_action_ko=_recommended_action(decision),
    )


def normalize_requested_mode(value: str | None) -> str:
    text = str(value or "OBSERVE").strip().upper().replace("-", "_")
    if text in {"LIVE_SIM", "LIVE_SIM_LIMITED", "SIM", "PAPER"}:
        return "LIVE_SIM_LIMITED"
    if text in {"DRY", "DRY_RUN"}:
        return "DRY_RUN"
    return "OBSERVE"


def pre_market_report_empty(*, requested_mode: str = "OBSERVE") -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return PreMarketCheckReport(
        trade_date=now.date().isoformat(),
        checked_at=now.isoformat(),
        requested_mode=normalize_requested_mode(requested_mode),
        go_no_go=PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED.value,
        summary_status=PreMarketCheckStatus.UNKNOWN.value,
        pass_count=0,
        warn_count=0,
        fail_count=0,
        unknown_count=1,
        skip_count=0,
        items=(
            PreMarketCheckItem(
                key="pre_market_check_missing",
                category="core",
                label_ko="Pre-market check",
                status=PreMarketCheckStatus.UNKNOWN.value,
                reason_code="PRE_MARKET_CHECK_MISSING",
                message_ko="장전 점검 결과가 아직 없습니다.",
                manual_review=True,
            ),
        ),
        warning_reasons=("PRE_MARKET_CHECK_MISSING",),
        operator_message_ko="수동 확인 필요",
        recommended_action_ko="장전 점검 API 또는 CLI를 먼저 실행하세요.",
    ).to_dict()


def _add_reboot_v2_items(
    add: Any,
    requested_mode: str,
    cfg: PreMarketCheckConfig,
    runtime: dict[str, Any],
    gateway: dict[str, Any],
    commands: dict[str, Any],
    sqlite_ok: bool,
    market_regime: dict[str, Any],
) -> None:
    profile = str(
        runtime.get("runtime_profile")
        or os.getenv("STRATEGY_RUNTIME_PROFILE")
        or os.getenv("STRATEGY_REBOOT_V2_PROFILE")
        or "V2_OBSERVE"
    ).upper()
    profile_is_reboot_v2 = _is_reboot_v2_profile(profile)
    v2_requested = cfg.strategy_reboot_v2_enabled or profile_is_reboot_v2
    if requested_mode != "OBSERVE" or not v2_requested:
        return
    pipeline_status = dict(runtime.get("pipeline_status") or {})
    top_level_enabled = cfg.strategy_reboot_v2_enabled and profile_is_reboot_v2
    add(
        "reboot_v2_top_level",
        "reboot_v2",
        "Reboot V2 top-level router",
        PreMarketCheckStatus.PASS if top_level_enabled else PreMarketCheckStatus.FAIL,
        reason_code="" if top_level_enabled else "REBOOT_V2_ROUTER_DISABLED",
        message_ko=f"profile={profile}, enabled={cfg.strategy_reboot_v2_enabled}",
        details={"runtime_profile": profile, "strategy_reboot_v2_enabled": cfg.strategy_reboot_v2_enabled},
    )
    for key, label in (
        ("candidate_ingestion", "CandidateIngestion"),
        ("candidate_hydrator", "CandidateHydrator"),
        ("theme_board", "ThemeBoard"),
        ("market_regime", "MarketRegime"),
        ("entry_engine", "EntryEngine"),
    ):
        _add_v2_component_item(add, key, label, pipeline_status, runtime.get(key))
    opening_enabled = bool(pipeline_status.get("opening_burst")) or bool(dict(runtime.get("opening_burst") or {}).get("enabled"))
    fallback_configured = _env_bool("TRADING_OPENING_BURST_FALLBACK_CONFIGURED", False)
    add(
        "v2_opening_burst_or_fallback",
        "reboot_v2",
        "Opening Burst or fallback",
        PreMarketCheckStatus.PASS if opening_enabled or fallback_configured else PreMarketCheckStatus.SKIP,
        reason_code="" if opening_enabled or fallback_configured else "CONFIG_DISABLED",
        message_ko="Opening Burst 또는 fallback 구성 확인",
        required=False,
        details={"opening_burst_enabled": opening_enabled, "fallback_configured": fallback_configured},
    )
    index_configured = bool(market_regime.get("index_watch_codes_configured") or runtime.get("index_watch_codes_configured"))
    add(
        "v2_index_watch_configured",
        "reboot_v2",
        "Index watch configured",
        PreMarketCheckStatus.PASS if index_configured else PreMarketCheckStatus.FAIL,
        reason_code="" if index_configured else "INDEX_WATCH_NOT_CONFIGURED",
        message_ko="KOSPI/KOSDAQ index watch 구성 확인",
    )
    add(
        "v2_sqlite_writable",
        "reboot_v2",
        "SQLite writable",
        PreMarketCheckStatus.PASS if sqlite_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if sqlite_ok else "SQLITE_NOT_WRITABLE",
        message_ko="SQLite writable 확인",
    )
    gateway_ok = bool(gateway.get("heartbeat_ok")) and bool(gateway.get("kiwoom_logged_in"))
    add(
        "v2_gateway_login_heartbeat",
        "reboot_v2",
        "Gateway heartbeat/login",
        PreMarketCheckStatus.PASS if gateway_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if gateway_ok else "GATEWAY_LOGIN_HEARTBEAT_NOT_READY",
        message_ko="Gateway heartbeat/login 확인",
    )
    acked = int(commands.get("acked_count") or commands.get("register_realtime_acked_count") or 0)
    add(
        "v2_subscription_command_ack",
        "reboot_v2",
        "Subscription command ACK",
        PreMarketCheckStatus.PASS if acked > 0 else PreMarketCheckStatus.FAIL,
        reason_code="" if acked > 0 else "SUBSCRIPTION_ACK_MISSING",
        message_ko="register_realtime ACK 확인",
        details={"acked_count": acked},
    )


def _is_reboot_v2_profile(profile: str) -> bool:
    normalized = str(profile or "").strip().upper()
    return normalized.startswith("V2_") or normalized in {"THEME_CORE_V3", "V3", "RT_TLS", "OPENING_THEME_BURST"}


def _add_v2_component_item(add: Any, key: str, label: str, pipeline_status: dict[str, Any], section: Any) -> None:
    data = dict(section or {}) if isinstance(section, dict) else {}
    enabled = bool(pipeline_status.get(key)) or bool(data.get("enabled"))
    add(
        f"v2_{key}_enabled",
        "reboot_v2",
        label,
        PreMarketCheckStatus.PASS if enabled else PreMarketCheckStatus.SKIP,
        reason_code="" if enabled else "CONFIG_DISABLED",
        message_ko=f"{label} enabled 확인" if enabled else f"{label} disabled",
        required=False if not enabled else True,
        details={"enabled": enabled, "status": data.get("status", "")},
    )


def _add_data_preload_items(
    add: Any,
    requested_mode: str,
    data_preload: dict[str, Any],
    theme_board: dict[str, Any],
    market_regime: dict[str, Any],
    cfg: PreMarketCheckConfig,
) -> None:
    theme_membership_loaded = bool(data_preload.get("theme_membership_loaded", False))
    symbol_master_loaded = bool(data_preload.get("symbol_master_loaded", False))
    prev_close_loaded = bool(data_preload.get("prev_close_loaded", data_preload.get("avg_turnover_loaded", False)))
    warehouse_status = str(data_preload.get("warehouse_preload_status") or data_preload.get("preload_status") or "").upper()
    local_cache_available = bool(data_preload.get("local_cache_available"))
    latest_theme_board = bool(theme_board.get("calculated_at") or theme_board.get("items") or theme_board.get("top_themes"))
    market_index_configured = bool(market_regime.get("index_watch_codes_configured", False))

    if warehouse_status in {"FAIL", "FAILED", "ERROR"} and local_cache_available:
        add(
            "warehouse_preload",
            "data",
            "Warehouse preload",
            PreMarketCheckStatus.WARN,
            reason_code="WAREHOUSE_PRELOAD_FAILED_LOCAL_CACHE_AVAILABLE",
            message_ko="외부 Warehouse preload 실패, local cache로 수동 확인 필요",
            manual_review=True,
            details=data_preload,
        )
    elif warehouse_status in {"FAIL", "FAILED", "ERROR"}:
        add(
            "warehouse_preload",
            "data",
            "Warehouse preload",
            PreMarketCheckStatus.FAIL if requested_mode == "LIVE_SIM_LIMITED" else PreMarketCheckStatus.WARN,
            reason_code="WAREHOUSE_PRELOAD_FAILED",
            message_ko="외부 Warehouse preload 실패",
            manual_review=requested_mode != "OBSERVE",
            details=data_preload,
        )
    else:
        add("warehouse_preload", "data", "Warehouse preload", PreMarketCheckStatus.PASS, message_ko="preload 장애 없음")

    for key, label, loaded, reason in (
        ("theme_membership", "Theme membership", theme_membership_loaded, "THEME_MEMBERSHIP_NOT_LOADED"),
        ("symbol_master", "Symbol master", symbol_master_loaded, "SYMBOL_MASTER_NOT_LOADED"),
        ("prev_close_turnover", "Prev close / avg turnover", prev_close_loaded, "PREV_CLOSE_TURNOVER_NOT_LOADED"),
    ):
        add(
            key,
            "data",
            label,
            PreMarketCheckStatus.PASS if loaded else PreMarketCheckStatus.WARN,
            reason_code="" if loaded else reason,
            message_ko=f"{label} 확인" if loaded else f"{label} preload 확인 필요",
            manual_review=not loaded and requested_mode != "OBSERVE",
        )
    add(
        "theme_board_latest",
        "data",
        "ThemeBoard latest",
        PreMarketCheckStatus.PASS if latest_theme_board else PreMarketCheckStatus.WARN,
        reason_code="" if latest_theme_board else "THEME_BOARD_WARMUP_WAIT",
        message_ko="ThemeBoard 최신 결과 확인" if latest_theme_board else "ThemeBoard warmup 전",
        manual_review=not latest_theme_board and requested_mode != "OBSERVE",
    )
    add(
        "market_regime_index_watch",
        "data",
        "MarketRegime index watch codes",
        PreMarketCheckStatus.PASS if market_index_configured else PreMarketCheckStatus.WARN,
        reason_code="" if market_index_configured else "MARKET_REGIME_INDEX_WATCH_MISSING",
        message_ko="시장지수 watch code 확인" if market_index_configured else "시장지수 watch code 확인 필요",
        manual_review=not market_index_configured and requested_mode != "OBSERVE",
    )
    add(
        "opening_burst_schedule",
        "data",
        "Opening Burst schedule",
        PreMarketCheckStatus.PASS if cfg.opening_burst_configured else PreMarketCheckStatus.SKIP,
        reason_code="" if cfg.opening_burst_configured else "CONFIG_DISABLED",
        message_ko="Opening Burst schedule 확인" if cfg.opening_burst_configured else "Opening Burst schedule 확인 필요",
        manual_review=not cfg.opening_burst_configured and requested_mode != "OBSERVE",
    )


def _add_risk_items(add: Any, requested_mode: str, cfg: PreMarketCheckConfig, risk: dict[str, Any], market_status: str) -> None:
    daily_loss_loaded = bool(risk.get("daily_loss_state_loaded", False))
    limits_ok = (
        cfg.max_order_quantity > 0
        and cfg.max_order_amount > 0
        and cfg.max_order_quantity <= cfg.live_sim_max_order_quantity_ceiling
        and cfg.max_order_amount <= cfg.live_sim_max_order_amount_ceiling
    )
    daily_limits_ok = cfg.max_daily_buy_orders > 0 and cfg.max_daily_sell_orders > 0
    open_position_limit_ok = cfg.max_open_positions > 0
    risk_off = market_status == "RISK_OFF"
    add(
        "daily_loss_state",
        "risk",
        "Daily loss state",
        PreMarketCheckStatus.PASS if daily_loss_loaded else PreMarketCheckStatus.WARN,
        reason_code="" if daily_loss_loaded else "DAILY_LOSS_STATE_NOT_LOADED",
        message_ko="일손실 상태 확인" if daily_loss_loaded else "일손실 상태 로드 확인 필요",
        manual_review=not daily_loss_loaded and requested_mode != "OBSERVE",
    )
    add(
        "max_order_limits",
        "risk",
        "Max quantity / amount",
        PreMarketCheckStatus.PASS if requested_mode != "LIVE_SIM_LIMITED" or limits_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if requested_mode != "LIVE_SIM_LIMITED" or limits_ok else "LIVE_SIM_ORDER_LIMIT_INVALID",
        message_ko=f"qty={cfg.max_order_quantity}, amount={cfg.max_order_amount}",
        required=requested_mode == "LIVE_SIM_LIMITED",
        details={
            "max_order_quantity": cfg.max_order_quantity,
            "max_order_amount": cfg.max_order_amount,
            "quantity_ceiling": cfg.live_sim_max_order_quantity_ceiling,
            "amount_ceiling": cfg.live_sim_max_order_amount_ceiling,
        },
    )
    add(
        "daily_order_limits",
        "risk",
        "Daily order limits",
        PreMarketCheckStatus.PASS if daily_limits_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if daily_limits_ok else "DAILY_ORDER_LIMIT_MISSING",
        message_ko=f"buy={cfg.max_daily_buy_orders}, sell={cfg.max_daily_sell_orders}",
        required=requested_mode == "LIVE_SIM_LIMITED",
    )
    add(
        "open_position_limit",
        "risk",
        "Open position limit",
        PreMarketCheckStatus.PASS if open_position_limit_ok else PreMarketCheckStatus.FAIL,
        reason_code="" if open_position_limit_ok else "OPEN_POSITION_LIMIT_MISSING",
        message_ko=f"max_open_positions={cfg.max_open_positions}",
        required=requested_mode == "LIVE_SIM_LIMITED",
    )
    add(
        "market_risk_off",
        "risk",
        "Market RISK_OFF",
        PreMarketCheckStatus.FAIL if requested_mode == "LIVE_SIM_LIMITED" and risk_off else (PreMarketCheckStatus.WARN if risk_off else PreMarketCheckStatus.PASS),
        reason_code="MARKET_RISK_OFF_BLOCK" if risk_off else "",
        message_ko="RISK_OFF 신규 매수 금지" if risk_off else "RISK_OFF 아님",
        manual_review=risk_off and requested_mode != "OBSERVE",
        required=requested_mode == "LIVE_SIM_LIMITED",
    )


def _go_no_go_decision(requested_mode: str, items: Iterable[PreMarketCheckItem]) -> PreMarketGoNoGoDecision:
    rows = list(items)
    if any(item.blocking for item in rows):
        return PreMarketGoNoGoDecision.NO_GO
    if requested_mode == "OBSERVE":
        return PreMarketGoNoGoDecision.GO_OBSERVE
    if any(item.manual_review or item.status == "UNKNOWN" for item in rows):
        return PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED
    if requested_mode == "LIVE_SIM_LIMITED":
        if any(item.status == "WARN" for item in rows):
            return PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED
        return PreMarketGoNoGoDecision.GO_LIVE_SIM_LIMITED
    return PreMarketGoNoGoDecision.GO_DRY_RUN


def _summary_status(decision: PreMarketGoNoGoDecision) -> str:
    if decision == PreMarketGoNoGoDecision.NO_GO:
        return PreMarketCheckStatus.FAIL.value
    if decision == PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED:
        return PreMarketCheckStatus.WARN.value
    return PreMarketCheckStatus.PASS.value


def _operator_message(decision: PreMarketGoNoGoDecision) -> str:
    return {
        PreMarketGoNoGoDecision.GO_OBSERVE: "관찰 전용 운영 가능",
        PreMarketGoNoGoDecision.GO_DRY_RUN: "DRY_RUN 운영 가능",
        PreMarketGoNoGoDecision.GO_LIVE_SIM_LIMITED: "모의주문 제한 운영 가능",
        PreMarketGoNoGoDecision.NO_GO: "운영 금지",
        PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED: "수동 확인 필요",
    }[decision]


def _recommended_action(decision: PreMarketGoNoGoDecision) -> str:
    return {
        PreMarketGoNoGoDecision.GO_OBSERVE: "주문 경로가 꺼진 상태에서 조건검색, TR, 실시간 데이터 흐름만 관찰하세요.",
        PreMarketGoNoGoDecision.GO_DRY_RUN: "DRY_RUN intent와 Dashboard V2를 관찰하되 Gateway 주문 전송은 활성화하지 마세요.",
        PreMarketGoNoGoDecision.GO_LIVE_SIM_LIMITED: "모의계좌, 1주, 금액 제한, 미체결 취소 스케줄러가 켜진 상태에서만 제한 운영하세요.",
        PreMarketGoNoGoDecision.NO_GO: "차단 사유를 해소하기 전에는 LIVE_SIM 주문도 시작하지 마세요.",
        PreMarketGoNoGoDecision.MANUAL_REVIEW_REQUIRED: "preload, warmup, reconcile 상태를 운영자가 확인한 뒤 낮은 모드로만 진행하세요.",
    }[decision]


def _order_manager_mode_status(requested_mode: str, cfg: PreMarketCheckConfig, order_manager: dict[str, Any]) -> PreMarketCheckStatus:
    mode = str(order_manager.get("mode") or cfg.order_manager_mode or "OBSERVE").upper()
    enabled = bool(order_manager.get("enabled", cfg.order_manager_enabled))
    allow_live_sim = bool(order_manager.get("live_sim_orders_allowed", cfg.allow_live_sim_orders))
    if requested_mode == "LIVE_SIM_LIMITED":
        return PreMarketCheckStatus.PASS if enabled and mode == "LIVE_SIM" and cfg.allow_live_sim_orders and allow_live_sim else PreMarketCheckStatus.FAIL
    if requested_mode == "DRY_RUN":
        return PreMarketCheckStatus.PASS if mode != "LIVE_SIM" and cfg.dry_run_entry_intents_enabled and cfg.dry_run_exit_sell_intents_enabled else PreMarketCheckStatus.FAIL
    if enabled and mode == "LIVE_SIM":
        return PreMarketCheckStatus.WARN
    return PreMarketCheckStatus.PASS


def _order_manager_mode_reason(requested_mode: str, cfg: PreMarketCheckConfig, order_manager: dict[str, Any]) -> str:
    status = _order_manager_mode_status(requested_mode, cfg, order_manager)
    if status == PreMarketCheckStatus.PASS:
        return ""
    if requested_mode == "LIVE_SIM_LIMITED":
        return "ORDER_MANAGER_LIVE_SIM_NOT_READY"
    if requested_mode == "DRY_RUN":
        return "DRY_RUN_INTENT_FLAGS_NOT_READY"
    return "ORDER_MANAGER_LIVE_SIM_ENABLED_DURING_OBSERVE"


def _broker_env(gateway: dict[str, Any], order_manager: dict[str, Any]) -> str:
    payload = dict(gateway.get("last_heartbeat_payload") or {})
    values = [
        order_manager.get("broker_env"),
        gateway.get("broker_env"),
        gateway.get("account_mode"),
        gateway.get("server_mode"),
        gateway.get("server_gubun"),
        payload.get("broker_env"),
        payload.get("account_mode"),
        payload.get("server_mode"),
        payload.get("server_gubun"),
    ]
    normalized = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
    if normalized & {"REAL", "PROD", "PRODUCTION", "LIVE", "LIVE_REAL", "0"}:
        return "REAL"
    if normalized & {"SIM", "SIMULATION", "MOCK", "PAPER", "DEMO", "LIVE_SIM", "1"}:
        return "SIMULATION"
    return "UNKNOWN"


def _account_whitelisted(account: str, cfg: PreMarketCheckConfig, order_manager: dict[str, Any]) -> bool:
    if "account_whitelisted" in order_manager:
        return bool(order_manager.get("account_whitelisted"))
    if not account:
        return False
    return not cfg.account_whitelist or account in cfg.account_whitelist


def _clean_time(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _status_value(status: PreMarketCheckStatus | str) -> str:
    if isinstance(status, PreMarketCheckStatus):
        return status.value
    text = str(status or "").upper()
    return text if text in {item.value for item in PreMarketCheckStatus} else PreMarketCheckStatus.UNKNOWN.value


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_to_dict(item) for item in value]
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, default)).strip()))
    except (TypeError, ValueError):
        return int(default)


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = str(os.getenv(name, default) or default).strip().upper()
    return raw if raw in allowed else default


__all__ = [
    "PreMarketCheckConfig",
    "PreMarketCheckItem",
    "PreMarketCheckReport",
    "PreMarketCheckStatus",
    "PreMarketGoNoGoDecision",
    "build_pre_market_check_report",
    "normalize_requested_mode",
    "pre_market_report_empty",
]
