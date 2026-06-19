from trading_app.dashboard_labels import reason_label_ko
from trading_app.dashboard_v2 import build_dashboard_v2_snapshot, dashboard_v2_auto_route_enabled, dashboard_v2_enabled


def test_dashboard_v2_is_default_enabled_and_auto_route(monkeypatch):
    monkeypatch.delenv("TRADING_DASHBOARD_V2_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_DASHBOARD_V2_AUTO_ROUTE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_DASHBOARD", raising=False)

    assert dashboard_v2_enabled() is True
    assert dashboard_v2_auto_route_enabled() is True


def test_dashboard_v2_empty_sections_return_stable_schema(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")

    payload = build_dashboard_v2_snapshot({}, detail="slim")

    assert payload["schema_version"] == "dashboard_v2.reboot_ops.v1"
    assert payload["enabled"] is True
    assert set(payload) >= {
        "v2_status",
        "market_overview",
        "leading_themes",
        "entry_candidates",
        "position_risk",
        "exit_watch",
        "order_manager",
        "wait_block_reasons",
        "system_health",
        "legacy_debug_link",
    }
    assert payload["leading_themes"]["items"] == []
    assert payload["entry_candidates"]["items"] == []


def test_dashboard_v2_prefers_active_runtime_reboot_sections(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "gateway": {"heartbeat_ok": True},
        "market_regime": {
            "status": "OK",
            "global_status": "DATA_WAIT",
            "data_wait_count": 1400,
            "calculated_at": "2026-06-18T09:00:00",
        },
        "runtime": {
            "mode": "DRY_RUN",
            "runtime_profile": "V2_OBSERVE",
            "reboot_v2_enabled": True,
            "data_warmup_status": "ready",
            "market_regime": {
                "enabled": True,
                "status": "OK",
                "global_status": "SELECTIVE",
                "kospi_status": "SELECTIVE",
                "kosdaq_status": "SELECTIVE",
                "data_wait_count": 0,
                "calculated_at": "2026-06-18T10:00:00",
            },
        },
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    assert payload["market_overview"]["global_status"] == "SELECTIVE"
    assert payload["v2_status"]["data_freshness_status"] == "FRESH"
    assert payload["system_health"]["data_freshness"] == "FRESH"
    assert payload["system_health"]["latest_market_regime_at"] == "2026-06-18T10:00:00"


def test_dashboard_v2_disabled_pipeline_is_not_data_wait(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "gateway": {"heartbeat_ok": True},
        "runtime": {
            "runtime_profile": "V2_OBSERVE",
            "reboot_v2_enabled": True,
            "pipeline_status": {"order_manager": False, "entry_engine": True},
            "order_manager": {"enabled": False, "status": "DISABLED", "blocking_reason": "ORDER_MANAGER_DISABLED_IN_V2_OBSERVE"},
            "entry_engine": {"enabled": True, "status": "OK", "calculated_at": "2026-06-18T09:03:00"},
            "data_warmup_status": "waiting_index",
        },
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    assert payload["v2_status"]["data_freshness_status"] == "FRESH"
    assert payload["v2_status"]["stages"]["order_manager"]["status"] == "DISABLED"
    assert payload["v2_status"]["stages"]["order_manager"]["blocking_reason"] == "ORDER_MANAGER_DISABLED_IN_V2_OBSERVE"


def test_dashboard_v2_limits_theme_board_to_top5(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "theme_board": {
            "status": "OK",
            "top_themes": [
                {"theme_rank": idx, "theme_name": f"테마{idx}", "theme_status": "LEADING_THEME", "theme_score": 100 - idx}
                for idx in range(1, 8)
            ],
        }
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    assert payload["leading_themes"]["top5_count"] == 5
    assert [item["theme_name"] for item in payload["leading_themes"]["items"]] == ["테마1", "테마2", "테마3", "테마4", "테마5"]
    assert payload["leading_themes"]["items"][0]["theme_status_label"] == "주도테마"


def test_dashboard_v2_entry_decisions_map_to_operator_buckets(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "entry_engine": {
            "status": "OK",
            "decisions": [
                {"code": "000001", "name": "READY", "entry_status": "OBSERVE_READY", "reason_codes": []},
                {"code": "000002", "name": "PRICE", "entry_status": "PRICE_WAIT", "reason_codes": ["CHASE_HIGH"]},
                {"code": "000003", "name": "WAIT", "entry_status": "DATA_WAIT", "reason_codes": ["LATEST_TICK_MISSING"]},
                {"code": "000004", "name": "BLOCK", "entry_status": "HARD_BLOCK", "reason_codes": ["MARKET_RISK_OFF_BLOCK"]},
            ],
        },
        "order_manager": {
            "managed_orders": [{"code": "000001", "status": "QUEUED_TO_GATEWAY"}],
        },
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    buckets = {item["code"]: item["display_bucket"] for item in payload["entry_candidates"]["items"]}
    assert buckets["000001"] == "ORDER_PENDING"
    assert buckets["000002"] == "SETUP_READY"
    assert buckets["000003"] == "WAIT"
    assert buckets["000004"] == "BLOCK"


def test_dashboard_v2_risk_off_and_real_broker_create_safety_banners(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "gateway": {
            "heartbeat_ok": True,
            "account": "12345678",
            "last_heartbeat_payload": {"server_gubun": "0"},
        },
        "market_regime": {
            "global_status": "RISK_OFF",
            "risk_off_detected": True,
            "systemic_risk_off": True,
            "block_new_entry_count": 3,
        },
        "order_manager": {
            "enabled": True,
            "mode": "LIVE_SIM",
            "broker_env": "REAL",
            "account": "12345678",
            "account_whitelisted": False,
            "live_sim_orders_allowed": False,
            "kill_switch_state": "KILL_SWITCH_ACTIVE",
        },
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    reasons = {item["reason_code"]: item for item in payload["safety_banners"]}
    assert reasons["REAL_BROKER_BLOCKED"]["severity"] == "critical"
    assert reasons["SYSTEMIC_RISK_OFF_BLOCK"]["severity"] == "critical"
    assert reasons["KILL_SWITCH_BLOCKS_BUY"]["severity"] == "critical"


def test_dashboard_v2_split_risk_off_is_warning_not_systemic_danger(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "gateway": {"heartbeat_ok": True},
        "market_regime": {
            "global_status": "RISK_OFF",
            "kospi_status": "EXPANSION",
            "kosdaq_status": "RISK_OFF",
            "composite_market_mode": "SPLIT_KOSPI_ON",
            "systemic_risk_off": False,
            "risk_off_detected": True,
            "candidate_policy_summary_by_side": {
                "KOSPI": {"total": 1, "ALLOW_REDUCED": 1},
                "KOSDAQ": {"total": 1, "BLOCK_NEW_ENTRY": 1},
            },
            "split_market_reduced_count": 1,
        },
        "order_manager": {"enabled": True, "mode": "LIVE_SIM", "live_sim_orders_allowed": True},
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    reasons = {item["reason_code"]: item for item in payload["safety_banners"]}
    assert payload["market_overview"]["systemic_risk_off"] is False
    assert payload["market_overview"]["composite_market_mode"] == "SPLIT_KOSPI_ON"
    assert payload["market_overview"]["split_market_reduced_count"] == 1
    assert "SYSTEMIC_RISK_OFF_BLOCK" not in reasons
    assert reasons["SPLIT_MARKET_HEALTHY_SIDE_REDUCED"]["severity"] == "warning"
    assert payload["v2_status"]["status_label"] != "위험"


def test_dashboard_v2_wait_block_reasons_aggregate_and_unknown_reason_fallback(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    snapshot = {
        "entry_engine": {
            "top_wait_reasons": [{"reason": "LATEST_TICK_MISSING", "count": 2}],
            "top_block_reasons": [{"reason": "UNKNOWN_NEW_REASON", "count": 1}],
        },
        "order_manager": {
            "top_wait_or_block_reasons": [{"reason": "ACCOUNT_NOT_WHITELISTED", "count": 1}],
        },
    }

    payload = build_dashboard_v2_snapshot(snapshot)

    reasons = {item["reason_code"]: item for item in payload["wait_block_reasons"]["items"]}
    assert reasons["LATEST_TICK_MISSING"]["reason_ko"] == "실시간 tick 대기"
    assert reasons["UNKNOWN_NEW_REASON"]["reason_ko"] == "UNKNOWN_NEW_REASON"
    assert reason_label_ko("UNKNOWN_NEW_REASON") == "UNKNOWN_NEW_REASON"
