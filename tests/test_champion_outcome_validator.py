from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import trading_app.api as api
from storage.db import TradingDatabase
from trading.strategy.champion_outcome_validator import (
    ChampionOutcomeValidatorConfig,
    ChampionOutcomeValidatorService,
    PRIMARY_ANCHOR_TYPE,
)
from trading.strategy.costs import RoundTripCostConfig, round_trip_cost_pct
from trading_app.dry_run_performance import DryRunPerformanceConfig, _round_trip_cost_pct


TRADE_DATE = "2026-06-22"


def test_contract_audit_blocks_when_pr2_contracts_missing():
    class EmptyDb:
        def __init__(self) -> None:
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row

    service = ChampionOutcomeValidatorService(EmptyDb(), config=_config())

    audit = service.audit_contracts()
    report = service.build_report(
        trade_date_from=TRADE_DATE,
        trade_date_to=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        persist=False,
    )

    assert audit["status"] == "BLOCKED_BY_PR2"
    assert "opportunity_benchmark_episodes" in audit["missing_tables"]
    assert report["status"] == "BLOCKED_BY_PR2"
    assert report["analysis_only"] is True
    assert report["recommendation"]["auto_apply_allowed"] is False


def test_build_report_persists_signal_outcomes_and_safety_flags(tmp_path):
    db = TradingDatabase(str(tmp_path / "champion.db"))
    _seed_linked_champion(db)

    report = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        trade_date_to=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        source_cutoff_at="2026-06-22T09:20:00",
        persist=True,
        rebuild_reason="unit test",
    )

    primary = [
        item
        for item in report["signal_outcomes"]
        if item["anchor_type"] == PRIMARY_ANCHOR_TYPE and item["horizon_min"] == 15
    ]
    assert len(report["signals"]) == 1
    assert primary[0]["label_status"] == "COMPLETE"
    assert primary[0]["barrier_outcome"] == "TARGET_FIRST"
    assert primary[0]["cost_adjusted_return_pct"] is not None
    assert report["analysis_only"] is True
    assert report["auto_apply_allowed"] is False
    assert report["dry_run_auto_enable_allowed"] is False
    assert report["order_safety"]["send_order"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM champion_signal_episodes").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM champion_signal_outcomes").fetchone()[0] > 0
    outcome_count = db.conn.execute("SELECT COUNT(*) FROM champion_signal_outcomes").fetchone()[0]
    ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        trade_date_to=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        source_cutoff_at="2026-06-22T09:20:00",
        persist=True,
        rebuild_reason="unit test replay",
    )
    assert db.conn.execute("SELECT COUNT(*) FROM champion_signal_outcomes").fetchone()[0] == outcome_count
    assert db.conn.execute("SELECT COUNT(*) FROM gateway_commands").fetchone()[0] == 0


def test_champion_first_forming_anchor_uses_setup_observation_price_without_benchmark_path(tmp_path):
    db = TradingDatabase(str(tmp_path / "forming-anchor.db"))
    db.save_candidate_funnel_episodes(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-forming",
                "candidate_id": 1,
                "candidate_generation_seq": 1,
                "code": "000001",
                "name": "Alpha",
                "max_stage_ordinal": 9,
                "current_stage": "CHAMPION_FORMING",
                "stop_stage": "CHAMPION_FORMING",
                "primary_reason": "LEADER_FIRST_PULLBACK_FORMING",
                "stop_reason_family": "CHAMPION_FORMING",
                "attribution_confidence": "HIGH",
                "baseline_role": "CHAMPION",
                "reached_stages": [
                    "SOURCE_DETECTED",
                    "CANDIDATE_CREATED",
                    "ACTIVE_SOURCE_PRESENT",
                    "HYDRATION_COMPLETE",
                    "EVALUATION_ELIGIBLE",
                    "REALTIME_SUBSCRIPTION_ACTIVE",
                    "FRESH_REALTIME_READY",
                    "STRATEGY_CONTEXT_READY",
                    "ENTRY_EVALUATED",
                    "CHAMPION_FORMING",
                ],
                "stage_first_reached_at": {
                    "FRESH_REALTIME_READY": "2026-06-22T09:01:00",
                    "ENTRY_EVALUATED": "2026-06-22T09:01:00",
                    "CHAMPION_FORMING": "2026-06-22T09:02:00",
                },
                "fingerprint": "cf-forming",
            }
        ]
    )
    db.save_setup_observations(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-forming",
                "candidate_id": 1,
                "code": "000001",
                "name": "Alpha",
                "calculated_at": "2026-06-22T09:02:00",
                "router_version": "setup_router_v3.5.2",
                "setup_type": "LEADER_FIRST_PULLBACK",
                "setup_generation": 1,
                "setup_instance_id": "setup-forming",
                "lifecycle_state": "FORMING",
                "shape_status": "FORMING",
                "context_status": "BLOCKED",
                "router_status": "CONTEXT_BLOCKED",
                "current_price": 1010,
                "primary_setup": True,
                "fingerprint": "setup-forming",
                "material_state_fingerprint": "setup-forming",
                "last_material_change_at": "2026-06-22T09:02:00",
            }
        ]
    )

    report = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        trade_date_to=TRADE_DATE,
        as_of="2026-06-22T09:03:00",
        source_cutoff_at="2026-06-22T09:03:00",
        persist=False,
    )
    forming = [
        item
        for item in report["signal_outcomes"]
        if item["anchor_type"] == "CHAMPION_FIRST_FORMING" and item["horizon_min"] == 15
    ]

    assert len(report["signals"]) == 1
    assert report["signals"][0]["first_forming_price"] == 1010
    assert forming[0]["anchor_price"] == 1010
    assert forming[0]["anchor_price_source"] == "SETUP_OBSERVATION_CURRENT_PRICE"


def test_price_less_expired_forming_signal_does_not_create_missing_anchor(tmp_path):
    db = TradingDatabase(str(tmp_path / "expired-forming-anchor.db"))
    db.save_candidate_funnel_episodes(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-forming",
                "candidate_id": 1,
                "candidate_generation_seq": 1,
                "code": "000001",
                "name": "Alpha",
                "max_stage_ordinal": 9,
                "current_stage": "CHAMPION_FORMING",
                "stop_stage": "CHAMPION_FORMING",
                "primary_reason": "LEADER_FIRST_PULLBACK_FORMING",
                "stop_reason_family": "CHAMPION_FORMING",
                "attribution_confidence": "HIGH",
                "baseline_role": "CHAMPION",
                "reached_stages": ["CHAMPION_FORMING"],
                "stage_first_reached_at": {"CHAMPION_FORMING": "2026-06-22T09:02:00"},
                "fingerprint": "cf-forming",
            }
        ]
    )
    db.save_setup_router_states(
        [
            {
                "trade_date": TRADE_DATE,
                "router_version": "setup_router_v3.5.2",
                "candidate_instance_id": "ci-forming",
                "candidate_id": 1,
                "code": "000001",
                "setup_type": "LEADER_FIRST_PULLBACK",
                "setup_generation": 1,
                "setup_instance_id": "setup-expired",
                "lifecycle_state": "EXPIRED",
                "detector_phase": "EXPIRED",
                "first_seen_at": "2026-06-22T09:01:00",
                "calculated_at": "2026-06-22T09:04:00",
                "expired_at": "2026-06-22T09:04:00",
                "terminal_at": "2026-06-22T09:04:00",
                "fingerprint": "setup-expired",
                "material_state_fingerprint": "setup-expired",
            }
        ]
    )
    db.save_setup_observations(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-forming",
                "candidate_id": 1,
                "code": "000001",
                "name": "Alpha",
                "calculated_at": "2026-06-22T09:02:00",
                "router_version": "setup_router_v3.5.2",
                "setup_type": "LEADER_FIRST_PULLBACK",
                "setup_generation": 1,
                "setup_instance_id": "setup-forming",
                "lifecycle_state": "FORMING",
                "shape_status": "FORMING",
                "context_status": "BLOCKED",
                "router_status": "CONTEXT_BLOCKED",
                "current_price": 1010,
                "primary_setup": True,
                "fingerprint": "setup-forming",
                "material_state_fingerprint": "setup-forming",
                "last_material_change_at": "2026-06-22T09:02:00",
            }
        ]
    )

    report = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        trade_date_to=TRADE_DATE,
        as_of="2026-06-22T09:05:00",
        source_cutoff_at="2026-06-22T09:05:00",
        persist=False,
    )
    forming_anchors = [item for item in report["anchors"] if item["anchor_type"] == "CHAMPION_FIRST_FORMING"]
    forming_outcomes = [item for item in report["signal_outcomes"] if item["anchor_type"] == "CHAMPION_FIRST_FORMING"]

    assert len(report["signals"]) == 2
    assert any(item["final_shape_status"] == "EXPIRED" for item in report["signals"])
    assert all(item["anchor_price"] > 0 for item in forming_anchors)
    assert all(item["anchor_price_source"] != "MISSING" for item in forming_anchors)
    assert len(forming_outcomes) == 1
    assert forming_outcomes[0]["anchor_price"] == 1010


def test_signal_identity_deduplicates_same_setup_and_splits_generation(tmp_path):
    db = TradingDatabase(str(tmp_path / "identity.db"))
    _seed_linked_champion(db)
    _seed_setup_observation(db, fingerprint="setup-1-duplicate")

    first = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        source_cutoff_at="2026-06-22T09:20:00",
        persist=False,
    )
    _seed_setup_state(db, setup_instance_id="setup-2", setup_generation=2, fingerprint="state-2")
    second = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        source_cutoff_at="2026-06-22T09:20:00",
        persist=False,
    )

    assert len(first["signals"]) == 1
    assert len(second["signals"]) == 2
    assert sorted(item["setup_generation"] for item in second["signals"]) == [1, 2]


def test_same_bar_ambiguity_is_labeled(tmp_path):
    db = TradingDatabase(str(tmp_path / "same-bar.db"))
    _seed_linked_champion(db, same_bar=True)

    report = ChampionOutcomeValidatorService(db, config=_config()).build_report(
        trade_date_from=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        source_cutoff_at="2026-06-22T09:20:00",
        persist=False,
    )

    primary = [
        item
        for item in report["signal_outcomes"]
        if item["anchor_type"] == PRIMARY_ANCHOR_TYPE and item["horizon_min"] == 15
    ]
    assert primary[0]["barrier_outcome"] == "AMBIGUOUS_SAME_BAR"
    assert report["valid_observe_metrics"]["ambiguous_count"] == 1


def test_runtime_section_builds_live_preview_without_persisted_report(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime-live-preview.db"))
    _seed_linked_champion(db)

    section = ChampionOutcomeValidatorService(db, config=_config()).runtime_section(
        trade_date=TRADE_DATE,
        as_of="2026-06-22T09:20:00",
        baseline=_baseline(),
    )

    assert section["status"] == "OK"
    assert section["report_id"]
    assert section["champion_matched_count"] == 1
    assert section["strict_labeled_signal_count"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM champion_outcome_reports").fetchone()[0] == 0


def test_cost_model_matches_dry_run_performance_helper():
    dry_config = DryRunPerformanceConfig(commission_bp_per_side=1.5, sell_tax_bp=15.0)
    shared = round_trip_cost_pct(
        RoundTripCostConfig(
            commission_bp_per_side=1.5,
            sell_tax_bp=15.0,
            entry_slippage_bp=10.0,
            exit_slippage_bp=10.0,
        )
    )

    assert shared == _round_trip_cost_pct(dry_config, 10.0)


def test_champion_api_rebuild_requires_token_and_never_changes_orders(monkeypatch, tmp_path):
    db_path = tmp_path / "api.db"
    db = TradingDatabase(str(db_path))
    _seed_linked_champion(db)
    db.close()
    monkeypatch.setenv("TRADING_CORE_TOKEN", "secret")
    monkeypatch.setattr(api, "open_database", lambda: TradingDatabase(str(db_path)))
    monkeypatch.setattr(api, "close_database", lambda db: db.close())
    monkeypatch.setattr(api.runtime_supervisor, "snapshot", lambda: {"strategy_baseline": _baseline()})
    client = TestClient(api.app)

    denied = client.post("/api/ops/champion-outcomes/rebuild", json={"rebuild_reason": "test"})
    allowed = client.post(
        "/api/ops/champion-outcomes/rebuild?trade_date=2026-06-22",
        json={"rebuild_reason": "test", "source_cutoff_at": "2026-06-22T09:20:00"},
        headers={"X-Local-Token": "secret"},
    )
    signals = client.get("/api/ops/champion-outcomes/signals?trade_date=2026-06-22").json()
    recommendation = client.get("/api/ops/champion-outcomes/recommendation?trade_date=2026-06-22").json()

    assert denied.status_code in {401, 403}
    assert allowed.status_code == 200
    assert allowed.json()["analysis_only"] is True
    assert allowed.json()["gateway_command_created"] is False
    assert allowed.json()["strategy_settings_changed"] is False
    assert allowed.json()["orders_created"] is False
    assert allowed.json()["opt10032_increment_count"] == 0
    assert allowed.json()["auto_apply_allowed"] is False
    assert signals["items"][0]["benchmark_link_status"] == "STRICT_LINKED"
    assert recommendation["recommendation"]["auto_apply_allowed"] is False


def _config(**overrides):
    data = {
        "enabled": True,
        "primary_horizon_min": 15,
        "horizons_min": (15,),
        "bootstrap_repetitions": 0,
    }
    data.update(overrides)
    return ChampionOutcomeValidatorConfig(**data)


def _baseline() -> dict:
    return {
        "baseline_id": "leader_first_pullback_v1",
        "baseline_version": "1.0.0",
        "config_hash": "hash",
        "git_sha": "abc1234567",
        "drift_status": "CLEAN",
        "config_snapshot_completeness": "FULL",
    }


def _seed_linked_champion(db: TradingDatabase, *, same_bar: bool = False) -> None:
    db.save_opportunity_benchmark_episodes(
        [
            {
                "benchmark_episode_id": "be-1",
                "schema_version": "test",
                "trade_date": TRADE_DATE,
                "code": "000001",
                "name": "Alpha",
                "generation_seq": 1,
                "first_seen_at": "2026-06-22T09:00:00",
                "last_seen_at": "2026-06-22T09:00:00",
                "active": False,
                "best_rank": 1,
                "max_turnover_krw": 10_000_000,
                "anchor_at": "2026-06-22T09:00:00",
                "anchor_price": 1000,
                "anchor_price_source": "TEST",
                "session_bucket": "OPENING",
                "market_side": "KOSDAQ",
                "episode_quality": "HIGH",
                "candidate_capture_status": "CAPTURED",
                "candidate_link_count": 1,
                "qualification_status": "VALID",
                "strict_sample_eligible": True,
                "fingerprint": "be-1",
            }
        ]
    )
    db.save_opportunity_benchmark_candidate_links(
        [
            {
                "link_id": "link-1",
                "trade_date": TRADE_DATE,
                "benchmark_episode_id": "be-1",
                "candidate_instance_id": "ci-1",
                "candidate_id": 1,
                "candidate_generation_seq": 1,
                "code": "000001",
                "benchmark_anchor_at": "2026-06-22T09:00:00",
                "candidate_first_seen_at": "2026-06-22T09:00:30",
                "detection_delay_sec": 30,
                "detection_window": "WITHIN_1M",
                "link_confidence": "HIGH",
                "primary_link": True,
            }
        ]
    )
    price_path = [
        _price("p-1", "2026-06-22T09:02:00", 1010, high=1010, low=1010),
        _price("p-2", "2026-06-22T09:17:00", 1030, high=1030, low=1005),
    ]
    if same_bar:
        price_path[1] = _price("p-2", "2026-06-22T09:17:00", 1010, high=1030, low=995)
    db.save_opportunity_benchmark_price_observations(price_path)
    db.save_opportunity_benchmark_outcomes(
        [
            {
                "outcome_id": "obo-1",
                "benchmark_episode_id": "be-1",
                "trade_date": TRADE_DATE,
                "code": "000001",
                "horizon_min": 15,
                "label_status": "COMPLETE",
                "label_quality": "HIGH",
                "return_pct": 2.0,
                "source_cutoff_at": "2026-06-22T09:20:00",
                "fingerprint": "obo-1",
            }
        ]
    )
    db.save_candidate_funnel_episodes(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-1",
                "candidate_id": 1,
                "candidate_generation_seq": 1,
                "code": "000001",
                "name": "Alpha",
                "max_stage_ordinal": 4,
                "current_stage": "CHAMPION_VALID_OBSERVE",
                "stop_stage": "",
                "primary_reason": "",
                "stop_reason_family": "",
                "attribution_confidence": "HIGH",
                "baseline_role": "CHAMPION",
                "reached_stages": [
                    "CHAMPION_FORMING",
                    "CHAMPION_MATCHED",
                    "CHAMPION_CONTEXT_ELIGIBLE",
                    "CHAMPION_VALID_OBSERVE",
                ],
                "stage_first_reached_at": {
                    "CHAMPION_FORMING": "2026-06-22T09:00:45",
                    "CHAMPION_MATCHED": "2026-06-22T09:01:00",
                    "CHAMPION_CONTEXT_ELIGIBLE": "2026-06-22T09:01:30",
                    "CHAMPION_VALID_OBSERVE": "2026-06-22T09:02:00",
                },
                "fingerprint": "cf-1",
            }
        ]
    )
    _seed_setup_observation(db)


def _seed_setup_observation(db: TradingDatabase, *, fingerprint: str = "setup-1") -> None:
    db.save_setup_observations(
        [
            {
                "trade_date": TRADE_DATE,
                "candidate_instance_id": "ci-1",
                "candidate_id": 1,
                "code": "000001",
                "name": "Alpha",
                "calculated_at": "2026-06-22T09:02:00",
                "router_version": "setup_router_v3.5.2",
                "setup_type": "LEADER_FIRST_PULLBACK",
                "setup_generation": 1,
                "setup_instance_id": "setup-1",
                "lifecycle_state": "MATCHED",
                "shape_status": "MATCHED",
                "context_status": "ELIGIBLE",
                "router_status": "VALID_OBSERVE",
                "entry_alignment_status": "ALIGNED",
                "primary_setup": True,
                "setup_quality_score": 0.8,
                "theme_id": "theme-1",
                "theme_name": "Theme",
                "theme_state": "LEADING",
                "stock_role": "LEADER",
                "market_side": "KOSDAQ",
                "market_action": "RISK_ON",
                "session_phase": "OPENING",
                "current_price": 1010,
                "reason_codes": ["OK"],
                "fingerprint": fingerprint,
                "material_state_fingerprint": fingerprint,
                "last_material_change_at": "2026-06-22T09:02:00",
            }
        ]
    )


def _seed_setup_state(
    db: TradingDatabase,
    *,
    setup_instance_id: str,
    setup_generation: int,
    fingerprint: str,
) -> None:
    db.save_setup_router_states(
        [
            {
                "trade_date": TRADE_DATE,
                "router_version": "setup_router_v3.5.2",
                "candidate_instance_id": "ci-1",
                "candidate_id": 1,
                "code": "000001",
                "theme_id": "theme-1",
                "setup_type": "LEADER_FIRST_PULLBACK",
                "setup_generation": setup_generation,
                "setup_instance_id": setup_instance_id,
                "lifecycle_state": "MATCHED",
                "detector_phase": "MATCHED",
                "state_entered_at": "2026-06-22T09:01:30",
                "first_seen_at": "2026-06-22T09:01:30",
                "calculated_at": "2026-06-22T09:03:00",
                "last_material_change_at": "2026-06-22T09:03:00",
                "reason_codes": ["OK"],
                "fingerprint": fingerprint,
                "observation_fingerprint": fingerprint,
                "material_state_fingerprint": fingerprint,
            }
        ]
    )


def _price(price_id: str, observed_at: str, price: float, *, high: float, low: float) -> dict:
    return {
        "price_observation_id": price_id,
        "benchmark_episode_id": "be-1",
        "trade_date": TRADE_DATE,
        "code": "000001",
        "observed_at": observed_at,
        "price": price,
        "high": high,
        "low": low,
        "volume": 1000,
        "source_type": "TEST",
        "source_resolution": "1m",
        "source_quality": "HIGH",
        "source_event_id": price_id,
        "source_fingerprint": price_id,
    }
