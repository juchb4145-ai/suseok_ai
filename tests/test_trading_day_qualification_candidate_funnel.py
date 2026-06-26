from datetime import datetime

from fastapi.testclient import TestClient

import trading_app.api as api
from storage.db import TradingDatabase
from trading.strategy.candidate_funnel import (
    CandidateFunnelService,
    TradingDayQualificationService,
    _reconcile_readiness_rows_with_active_lifecycle,
)
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState


TRADE_DATE = "2026-06-22"
NOW = datetime(2026, 6, 22, 9, 5, 0)


def test_candidate_funnel_counts_episode_once_and_champion_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "funnel.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id)
    _seed_context_entry(db, candidate.id)
    _seed_setup_observation(db, candidate.id, setup_type="LEADER_FIRST_PULLBACK", shape_status="MATCHED", router_status="VALID_OBSERVE", context_status="ELIGIBLE")
    _seed_setup_observation(db, candidate.id, setup_type="VWAP_RECLAIM", shape_status="MATCHED", router_status="VALID_OBSERVE", context_status="ELIGIBLE")
    db.save_entry_decisions([{**_entry_decision(candidate.id), "calculated_at": "2026-06-22T09:05:01"}])

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=True)

    assert report["candidate_episode_count"] == 1
    assert report["strict_attribution_count"] == 1
    assert report["low_confidence_attribution_count"] == 0
    assert _stage_count(report, "ENTRY_EVALUATED") == 1
    assert _stage_count(report, "CHAMPION_FORMING") == 1
    assert _stage_count(report, "CHAMPION_MATCHED") == 1
    assert _stage_count(report, "CHAMPION_CONTEXT_ELIGIBLE") == 1
    assert _stage_count(report, "CHAMPION_VALID_OBSERVE") == 1
    assert _stage(report, "ORDER_INTENT_CREATED")["applicable"] is False
    assert report["no_trade_classification"]["classification"] == "EXPECTED_OBSERVE_ONLY"
    assert len(db.list_candidate_funnel_episodes(trade_date=TRADE_DATE)) == 1


def test_candidate_funnel_low_confidence_source_episode_and_invariant_detection(tmp_path):
    db = TradingDatabase(str(tmp_path / "low.db"))
    db.save_candidate_source_event(
        {
            "trade_date": TRADE_DATE,
            "code": "000002",
            "source_type": "opening_burst",
            "detected_at": "2026-06-22T09:01:00",
        }
    )
    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)

    assert report["candidate_episode_count"] == 1
    assert report["strict_attribution_count"] == 0
    assert report["low_confidence_attribution_count"] == 1
    assert report["episodes"][0]["attribution_confidence"] == "LOW"


def test_candidate_funnel_flags_future_and_negative_stage_timestamps(tmp_path):
    db = TradingDatabase(str(tmp_path / "invariant.db"))
    future = _seed_candidate(db, code="000002", instance_id="ci-future", detected_at="2026-06-22T09:06:00")
    negative = _seed_candidate(db, code="000003", instance_id="ci-negative")
    _seed_readiness(db, negative.id, code="000003", instance_id="ci-negative")
    _seed_context_entry(db, negative.id, code="000003")
    _seed_setup_observation(
        db,
        negative.id,
        code="000003",
        instance_id="ci-negative",
        shape_status="MATCHED",
        router_status="VALID_OBSERVE",
        context_status="ELIGIBLE",
        calculated_at="2026-06-22T09:04:00",
    )

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)
    types = {item["type"] for item in report["invariant_violations"]}

    assert future.id is not None
    assert "FUTURE_TIMESTAMP" in types
    assert "NEGATIVE_STAGE_LATENCY" in types
    assert report["critical_invariant_violation_count"] >= 2


def test_candidate_funnel_context_entry_without_realtime_is_not_critical_invariant(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-entry-only.db"))
    candidate = _seed_candidate(db)
    _seed_context_entry(db, candidate.id)

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)

    assert _stage_count(report, "STRATEGY_CONTEXT_READY") == 1
    assert _stage_count(report, "ENTRY_EVALUATED") == 1
    assert report["critical_invariant_violation_count"] == 0
    assert all(
        item.get("missing_stage") not in {"REALTIME_SUBSCRIPTION_ACTIVE", "FRESH_REALTIME_READY"}
        for item in report["invariant_violations"]
    )


def test_candidate_funnel_missing_root_is_deduped_and_escalated_to_critical(tmp_path):
    db = TradingDatabase(str(tmp_path / "missing-root.db"))
    candidate = _seed_candidate(db)
    _seed_context_entry(db, candidate.id)
    _seed_setup_observation(db, candidate.id, shape_status="MATCHED", router_status="VALID_OBSERVE", context_status="ELIGIBLE")

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)
    fresh_missing = [
        item
        for item in report["invariant_violations"]
        if item.get("candidate_instance_id") == candidate.metadata["candidate_instance_id"]
        and item.get("missing_stage") == "FRESH_REALTIME_READY"
    ]

    assert len(fresh_missing) == 1
    assert fresh_missing[0]["severity"] == "CRITICAL"
    assert "CHAMPION_VALID_OBSERVE" in fresh_missing[0]["blocked_stages"]


def test_candidate_identity_same_code_new_instance_is_separate_episode(tmp_path):
    db = TradingDatabase(str(tmp_path / "identity.db"))
    _seed_readiness(db, 101, code="000004", instance_id="ci-old")
    _seed_candidate(db, code="000004", instance_id="ci-new")

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)
    ids = {item["candidate_instance_id"] for item in report["episodes"]}

    assert report["candidate_episode_count"] == 2
    assert report["strict_attribution_count"] == 2
    assert ids == {"ci-old", "ci-new"}


def test_candidate_funnel_reconciles_active_fresh_lifecycle_to_current_readiness(tmp_path):
    db = TradingDatabase(str(tmp_path / "lifecycle-current.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id, active=True, fresh=False, candle_ready=True)
    _seed_lifecycle_active_fresh(db)

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=True)
    qualification = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        runtime_snapshot=_runtime_snapshot(),
        funnel_report=report,
        persist=False,
    )

    assert _stage_count(report, "FRESH_REALTIME_READY") == 1
    assert "FRESH_REALTIME_READY" in report["episodes"][0]["reached_stages"]
    assert qualification["evidence_summary"]["fresh_realtime_ready_count"] == 1
    assert qualification["evidence_summary"]["readiness_wait_count"] == 0
    readiness = db.list_setup_router_readiness_latest(trade_date=TRADE_DATE, code="000001")[0]
    assert readiness["candidate_instance_id"] == "ci-000001"
    assert readiness["readiness_ready"] is True
    assert readiness["post_subscription_tick_verified"] is True


def test_candidate_funnel_reconciles_active_fresh_when_old_readiness_row_exists(tmp_path):
    db = TradingDatabase(str(tmp_path / "lifecycle-old-row.db"))
    candidate = _seed_candidate(db, code="000002", instance_id="ci-new")
    _seed_readiness(db, 999, active=True, fresh=False, code="000002", instance_id="ci-old")
    _seed_lifecycle_active_fresh(db, code="000002")

    report = CandidateFunnelService(db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)
    by_ci = {item["candidate_instance_id"]: item for item in report["episodes"]}

    assert candidate.id is not None
    assert _stage_count(report, "FRESH_REALTIME_READY") == 1
    assert "FRESH_REALTIME_READY" in by_ci["ci-new"]["reached_stages"]
    assert "FRESH_REALTIME_READY" not in by_ci["ci-old"]["reached_stages"]


def test_candidate_funnel_uses_setup_observation_readiness_for_champion_forming_latency(tmp_path):
    db = TradingDatabase(str(tmp_path / "observation-readiness.db"))
    candidate = _seed_candidate(db, last_seen_at="2026-06-22T09:06:00")
    _seed_context_entry(db, candidate.id)
    _seed_setup_observation(
        db,
        candidate.id,
        shape_status="FORMING",
        router_status="CONTEXT_BLOCKED",
        context_status="BLOCKED",
        current_price=1010,
        post_subscription_tick_verified=True,
        input_readiness_calculated_at="2026-06-22T09:05:00",
    )

    report = CandidateFunnelService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=datetime(2026, 6, 22, 9, 7, 0),
        baseline=_baseline(),
        persist=False,
    )
    episode = next(item for item in report["episodes"] if item["candidate_instance_id"] == "ci-000001")

    assert "FRESH_REALTIME_READY" in episode["reached_stages"]
    assert "CHAMPION_FORMING" in episode["reached_stages"]
    assert episode["stage_first_reached_at"]["FRESH_REALTIME_READY"] == "2026-06-22T09:05:00"
    assert not [
        item
        for item in report["invariant_violations"]
        if item.get("candidate_instance_id") == "ci-000001"
        and item.get("type") == "NEGATIVE_STAGE_LATENCY"
        and item.get("stage") == "CHAMPION_FORMING"
    ]


def test_active_fresh_lifecycle_reconcile_is_ambiguous_for_multiple_current_candidates():
    episodes = [
        _episode_row("ci-a", "000003"),
        _episode_row("ci-b", "000003"),
    ]
    rows = _reconcile_readiness_rows_with_active_lifecycle(
        [],
        [_lifecycle_row(code="000003")],
        episodes,
        as_of=NOW,
    )

    assert rows == []


def test_active_fresh_lifecycle_reconcile_requires_post_active_tick():
    rows = _reconcile_readiness_rows_with_active_lifecycle(
        [],
        [_lifecycle_row(code="000003", last_tick_at="2026-06-22T09:03:59Z")],
        [_episode_row("ci-a", "000003")],
        as_of=NOW,
    )

    assert rows == []


def test_active_fresh_lifecycle_reconcile_preserves_non_realtime_blockers():
    rows = _reconcile_readiness_rows_with_active_lifecycle(
        [
            {
                "trade_date": TRADE_DATE,
                "router_version": "setup_router_v3.5.2",
                "candidate_instance_id": "ci-a",
                "code": "000003",
                "readiness_status": "WAIT_REGISTER_COMMAND",
                "readiness_ready": False,
                "candle_ready": False,
                "market_context_fresh": True,
                "theme_context_fresh": True,
            }
        ],
        [_lifecycle_row(code="000003")],
        [_episode_row("ci-a", "000003")],
        as_of=NOW,
    )

    assert rows[0]["post_subscription_tick_verified"] is True
    assert rows[0]["readiness_ready"] is False
    assert rows[0]["readiness_status"] == "WAIT_CANDLE_WARMUP"
    assert "COMPLETED_1M_CANDLES_INSUFFICIENT" in rows[0]["reason_codes"]


def test_trading_day_qualification_valid_final_with_clean_baseline(tmp_path):
    db = TradingDatabase(str(tmp_path / "valid.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id)
    _seed_context_entry(db, candidate.id)
    _seed_setup_observation(db, candidate.id, shape_status="MATCHED", router_status="VALID_OBSERVE", context_status="ELIGIBLE")

    service = TradingDayQualificationService(db)
    report = service.build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=True,
    )

    assert report["qualification_status"] == "VALID"
    assert report["strict_sample_eligible"] is True
    assert report["baseline_id"] == "leader_first_pullback_v1"
    assert report["config_hash"] == "hash-clean"
    assert report["no_trade_classification"]["classification"] == "EXPECTED_OBSERVE_ONLY"
    assert report["revision"] == 1


def test_trading_day_qualification_live_preview_collecting(tmp_path):
    db = TradingDatabase(str(tmp_path / "preview.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id)

    report = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="LIVE_PREVIEW",
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    assert report["report_state"] == "LIVE_PREVIEW"
    assert report["qualification_status"] == "COLLECTING"
    assert report["strict_sample_eligible"] is False


def test_trading_day_qualification_baseline_drift_and_partial_are_invalid(tmp_path):
    db = TradingDatabase(str(tmp_path / "baseline-invalid.db"))
    drift = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot({**_baseline(), "drift_status": "DRIFT_DETECTED"}),
        persist=False,
    )
    partial = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot({**_baseline(), "config_snapshot_completeness": "PARTIAL"}),
        persist=False,
    )

    assert drift["qualification_status"] == "INVALID"
    assert partial["qualification_status"] == "INVALID"


def test_market_context_and_realtime_thresholds(tmp_path):
    db = TradingDatabase(str(tmp_path / "quality.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id, active=False, fresh=False)
    for index in range(3):
        db.save_ops_runtime_health_sample(
            {
                "trade_date": TRADE_DATE,
                "bucket_at": f"2026-06-22T09:0{index}:00",
                "sampled_at": f"2026-06-22T09:0{index}:00",
                "market_context_source": "UNAVAILABLE",
                "market_context_available": False,
            }
        )

    report = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    assert report["qualification_status"] == "INVALID"
    assert "MARKET_CONTEXT_UNAVAILABLE_CONSECUTIVE_INVALID" in report["reason_codes"]
    assert "SUBSCRIPTION_COVERAGE_INVALID" in report["reason_codes"]


def test_market_context_degraded_and_fallback_thresholds(tmp_path):
    degraded_db = TradingDatabase(str(tmp_path / "market-degraded.db"))
    for index in range(20):
        _save_health_sample(degraded_db, index, source="PIPELINE_VIEW")
    _save_health_sample(degraded_db, 21, source="UNAVAILABLE", available=False)

    degraded = TradingDayQualificationService(degraded_db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    fallback_warn_db = TradingDatabase(str(tmp_path / "fallback-warn.db"))
    for index in range(20):
        _save_health_sample(fallback_warn_db, index, source="PIPELINE_VIEW")
    for index in range(20, 23):
        _save_health_sample(fallback_warn_db, index, source="DB_FALLBACK")
    fallback_warn = TradingDayQualificationService(fallback_warn_db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    fallback_invalid_db = TradingDatabase(str(tmp_path / "fallback-invalid.db"))
    for index in range(20):
        _save_health_sample(fallback_invalid_db, index, source="PIPELINE_VIEW")
    for index in range(20, 30):
        _save_health_sample(fallback_invalid_db, index, source="DASHBOARD_SUMMARY_FALLBACK")
    fallback_invalid = TradingDayQualificationService(fallback_invalid_db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    assert degraded["qualification_status"] == "DEGRADED"
    assert "MARKET_CONTEXT_UNAVAILABLE" in degraded["reason_codes"]
    assert fallback_warn["qualification_status"] == "DEGRADED"
    assert "MARKET_CONTEXT_FALLBACK_RATE_WARN" in fallback_warn["reason_codes"]
    assert fallback_invalid["qualification_status"] == "INVALID"
    assert "MARKET_CONTEXT_FALLBACK_RATE_INVALID" in fallback_invalid["reason_codes"]


def test_snapshot_integrity_regression_conflict_and_namespace_invalid(tmp_path):
    db = TradingDatabase(str(tmp_path / "snapshot-invalid.db"))
    _save_health_sample(db, 0, generation=2, checksum="a")
    _save_health_sample(db, 1, generation=1, checksum="b")
    _save_health_sample(db, 2, generation=3, checksum="c", namespace="legacy")
    _save_health_sample(db, 3, generation=4, checksum="d")
    _save_health_sample(db, 4, generation=4, checksum="e")

    report = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=datetime(2026, 6, 22, 9, 10, 0),
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    assert report["qualification_status"] == "INVALID"
    assert "SNAPSHOT_GENERATION_REGRESSION" in report["reason_codes"]
    assert "SNAPSHOT_CHECKSUM_CONFLICT" in report["reason_codes"]
    assert "SNAPSHOT_NAMESPACE_MISMATCH" in report["reason_codes"]


def test_observe_order_safety_violation_invalidates_day(tmp_path):
    db = TradingDatabase(str(tmp_path / "order-violation.db"))
    _insert_runtime_order_intent(db)

    report = TradingDayQualificationService(db).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        report_state="FINAL",
        finalize=True,
        runtime_snapshot=_runtime_snapshot(),
        persist=False,
    )

    assert report["qualification_status"] == "INVALID"
    assert "OBSERVE_ORDER_ACTIVITY_RUNTIME_ORDER_INTENTS" in report["reason_codes"]


def test_no_trade_classification_boundaries_do_not_assert_future_pr_labels(tmp_path):
    healthy_db = TradingDatabase(str(tmp_path / "healthy-no-champion.db"))
    healthy_candidate = _seed_candidate(healthy_db)
    _seed_readiness(healthy_db, healthy_candidate.id)
    _seed_context_entry(healthy_db, healthy_candidate.id)
    healthy = CandidateFunnelService(healthy_db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)

    blocked_db = TradingDatabase(str(tmp_path / "context-blocked.db"))
    blocked_candidate = _seed_candidate(blocked_db)
    _seed_readiness(blocked_db, blocked_candidate.id)
    _seed_context_entry(blocked_db, blocked_candidate.id)
    _seed_setup_observation(blocked_db, blocked_candidate.id, shape_status="MATCHED", router_status="PENDING", context_status="WAIT")
    blocked = CandidateFunnelService(blocked_db).build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=False)

    empty = CandidateFunnelService(TradingDatabase(str(tmp_path / "empty.db"))).build_report(
        trade_date=TRADE_DATE,
        as_of=NOW,
        baseline=_baseline(),
        persist=False,
    )

    labels = {
        healthy["no_trade_classification"]["classification"],
        blocked["no_trade_classification"]["classification"],
        empty["no_trade_classification"]["classification"],
    }

    assert healthy["no_trade_classification"]["classification"] == "HEALTHY_NO_CHAMPION_SIGNAL"
    assert healthy["no_trade_classification"]["requires_opportunity_benchmark"] is True
    assert blocked["no_trade_classification"]["classification"] == "CHAMPION_CONTEXT_BLOCKED"
    assert blocked["no_trade_classification"]["requires_outcome_labels"] is True
    assert empty["no_trade_classification"]["classification"] == "DISCOVERY_COVERAGE_UNKNOWN"
    assert "DISCOVERY_MISS" not in labels
    assert "OVERFILTERED" not in labels


def test_persistence_preview_upsert_final_revision_and_episode_write_skip(tmp_path):
    db = TradingDatabase(str(tmp_path / "persist.db"))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id)
    service = CandidateFunnelService(db)
    first = service.build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=True)
    second = service.build_report(trade_date=TRADE_DATE, as_of=NOW, baseline=_baseline(), persist=True)

    assert first["report_id"] == second["report_id"]
    assert db.conn.execute("SELECT COUNT(*) FROM candidate_funnel_episode_latest").fetchone()[0] == 1

    q = TradingDayQualificationService(db)
    preview1 = q.build_report(trade_date=TRADE_DATE, as_of=NOW, runtime_snapshot=_runtime_snapshot(), persist=True)
    preview2 = q.build_report(trade_date=TRADE_DATE, as_of=NOW, runtime_snapshot=_runtime_snapshot(), persist=True)
    final1 = q.build_report(trade_date=TRADE_DATE, as_of=NOW, report_state="FINAL", finalize=True, runtime_snapshot=_runtime_snapshot(), persist=True)
    final2 = q.build_report(trade_date=TRADE_DATE, as_of=NOW, report_state="FINAL", finalize=True, runtime_snapshot=_runtime_snapshot(), persist=True)

    assert preview1["report_id"] == preview2["report_id"]
    assert final1["revision"] == 1
    assert final2["revision"] == 2


def test_candidate_funnel_api_rebuild_requires_token_and_filters(monkeypatch, tmp_path):
    db_path = tmp_path / "api.db"
    db = TradingDatabase(str(db_path))
    candidate = _seed_candidate(db)
    _seed_readiness(db, candidate.id)
    db.close()
    monkeypatch.setenv("TRADING_CORE_TOKEN", "secret")
    monkeypatch.setattr(api, "open_database", lambda: TradingDatabase(str(db_path)))
    monkeypatch.setattr(api, "close_database", lambda db: db.close())
    monkeypatch.setattr(api.runtime_supervisor, "snapshot", lambda: _runtime_snapshot())
    client = TestClient(api.app)

    denied = client.post("/api/ops/candidate-funnel/rebuild", json={"rebuild_reason": "test"})
    allowed = client.post(
        "/api/ops/candidate-funnel/rebuild?trade_date=2026-06-22",
        json={"rebuild_reason": "test"},
        headers={"X-Local-Token": "secret"},
    )
    episodes = client.get("/api/ops/candidate-funnel/episodes?trade_date=2026-06-22&attribution_confidence=HIGH").json()
    detail = client.get("/api/ops/candidate-funnel/candidates/ci-000001?trade_date=2026-06-22").json()
    missing = client.get("/api/ops/candidate-funnel/candidates/missing?trade_date=2026-06-22")

    assert denied.status_code in {401, 403}
    assert allowed.status_code == 200
    assert allowed.json()["analysis_only"] is True
    assert episodes["items"][0]["candidate_instance_id"] == "ci-000001"
    assert detail["candidate_instance_id"] == "ci-000001"
    assert missing.status_code == 404


def _seed_candidate(
    db,
    *,
    code="000001",
    instance_id="ci-000001",
    detected_at="2026-06-22T09:01:00",
    last_seen_at="2026-06-22T09:05:00",
):
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code=code,
            name="테스트",
            state=CandidateState.WATCHING,
            sources=[CandidateSourceType.CONDITION_SEARCH],
            detected_at=detected_at,
            last_seen_at=last_seen_at,
            metadata={"candidate_instance_id": instance_id, "candidate_generation_seq": 1},
        )
    )


def _seed_readiness(db, candidate_id, *, active=True, fresh=True, code="000001", instance_id="ci-000001", candle_ready=None):
    if candle_ready is None:
        candle_ready = fresh
    db.save_setup_router_readiness_snapshots(
        [
            {
                "trade_date": TRADE_DATE,
                "router_version": "setup_router_v3.5.2",
                "candidate_instance_id": instance_id,
                "candidate_id": candidate_id,
                "code": code,
                "name": "테스트",
                "readiness_status": "READY" if active and fresh else "WAIT_SUBSCRIPTION_NOT_ACTIVE",
                "readiness_ready": active and fresh,
                "subscription_selected": True,
                "subscription_target_selected": True,
                "subscription_active": active,
                "subscription_active_since": "2026-06-22T09:03:00" if active else "",
                "latest_tick_at": "2026-06-22T09:04:59" if fresh else "",
                "latest_tick_age_sec": 1.0 if fresh else 999.0,
                "latest_tick_source": "REALTIME" if fresh else "TR_BACKFILL",
                "post_subscription_tick_verified": fresh,
                "candle_ready": candle_ready,
                "calculated_at": "2026-06-22T09:05:00",
            }
        ]
    )


def _seed_lifecycle_active_fresh(db, *, code="000001"):
    db.save_realtime_subscription_lifecycle_latest(_lifecycle_row(code=code))


def _lifecycle_row(*, code="000001", last_tick_at="2026-06-22T09:04:10Z"):
    return {
        "trade_date": TRADE_DATE,
        "code": code,
        "schema_version": "realtime_subscription_lifecycle.v1",
        "lifecycle_state": "ACTIVE_FRESH",
        "requested": True,
        "target_selected": True,
        "budget_deferred": False,
        "command_enqueued": True,
        "command_dispatched": True,
        "acked": True,
        "transport_active": True,
        "first_tick_verified": True,
        "decision_fresh": True,
        "stale": False,
        "released": False,
        "failed": False,
        "screen_no": "7000",
        "register_command_id": "cmd-register",
        "subscription_generation": 1,
        "requested_at_utc": "2026-06-22T09:03:00Z",
        "target_selected_at_utc": "2026-06-22T09:03:00Z",
        "command_enqueued_at_utc": "2026-06-22T09:03:01Z",
        "command_dispatched_at_utc": "2026-06-22T09:03:02Z",
        "command_acked_at_utc": "2026-06-22T09:03:03Z",
        "registration_ack_baseline_at_utc": "2026-06-22T09:04:00Z",
        "first_tick_at_utc": last_tick_at,
        "last_tick_at_utc": last_tick_at,
        "latest_tick_age_sec": 1.0,
        "updated_at_utc": last_tick_at,
    }


def _episode_row(candidate_instance_id, code):
    return {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": candidate_instance_id,
        "candidate_id": None,
        "candidate_generation_seq": 1,
        "code": code,
        "name": "테스트",
        "reached_stages": ["SOURCE_DETECTED", "CANDIDATE_CREATED", "ACTIVE_SOURCE_PRESENT", "HYDRATION_COMPLETE", "EVALUATION_ELIGIBLE"],
    }


def _seed_context_entry(db, candidate_id, *, code="000001"):
    db.save_strategy_context_snapshot(
        {
            "trade_date": TRADE_DATE,
            "code": code,
            "candidate_id": candidate_id,
            "context_id": "ctx-1",
            "calculated_at": "2026-06-22T09:05:00",
            "context_fresh": True,
            "session_phase": "OPENING",
            "reason_codes": [],
        }
    )
    db.save_entry_decisions([_entry_decision(candidate_id, code=code)])


def _entry_decision(candidate_id, *, code="000001"):
    return {
        "trade_date": TRADE_DATE,
        "candidate_id": candidate_id,
        "code": code,
        "name": "테스트",
        "calculated_at": "2026-06-22T09:05:00",
        "entry_status": "OBSERVE_READY",
        "ready_allowed": True,
        "reason_codes": [],
    }


def _seed_setup_observation(
    db,
    candidate_id,
    *,
    setup_type="LEADER_FIRST_PULLBACK",
    shape_status="FORMING",
    router_status="PENDING",
    context_status="WAIT",
    code="000001",
    instance_id="ci-000001",
    calculated_at="2026-06-22T09:05:00",
    current_price=0,
    post_subscription_tick_verified=False,
    input_readiness_calculated_at="",
):
    db.save_setup_observations(
        [
            {
                "trade_date": TRADE_DATE,
                "router_version": "setup_router_v3.5.2",
                "candidate_instance_id": instance_id,
                "candidate_id": candidate_id,
                "code": code,
                "name": "테스트",
                "calculated_at": calculated_at,
                "setup_type": setup_type,
                "shape_status": shape_status,
                "router_status": router_status,
                "context_status": context_status,
                "baseline_role": "CHAMPION" if setup_type == "LEADER_FIRST_PULLBACK" else "CHALLENGER",
                "primary_setup": setup_type == "LEADER_FIRST_PULLBACK",
                "setup_quality_score": 80.0,
                "current_price": current_price,
                "post_subscription_tick_verified": post_subscription_tick_verified,
                "input_readiness_calculated_at": input_readiness_calculated_at,
                "fingerprint": f"{setup_type}:{shape_status}:{router_status}:{context_status}",
                "observation_fingerprint": f"{setup_type}:{shape_status}:{router_status}:{context_status}",
                "material_state_fingerprint": f"{setup_type}:{shape_status}:{router_status}:{context_status}",
                "reason_codes": [],
            }
        ]
    )


def _baseline():
    return {
        "enabled": True,
        "status": "FROZEN",
        "baseline_id": "leader_first_pullback_v1",
        "baseline_version": "1.0.0",
        "version": "1.0.0",
        "runtime_profile": "THEME_CORE_V3",
        "drift_status": "CLEAN",
        "config_snapshot_completeness": "COMPLETE",
        "config_hash": "hash-clean",
        "git_sha": "abcdef123456",
        "git_dirty_or_unknown": False,
        "strategy_mutation_allowed": False,
        "order_intent_allowed": False,
        "live_order_allowed": False,
    }


def _runtime_snapshot(baseline=None):
    return {
        "runtime_profile": "THEME_CORE_V3",
        "cycle_count": 1,
        "status": "OK",
        "strategy_baseline": dict(baseline or _baseline()),
        "market_context_transport": {"source": "PIPELINE_VIEW"},
        "pipeline_status": {
            "market_regime": True,
            "theme_board": True,
            "strategy_context": True,
            "setup_router_v3": True,
        },
    }


def _stage(report, stage):
    return next(item for item in report["stages"] if item["stage"] == stage)


def _stage_count(report, stage):
    return _stage(report, stage)["strict_reached_count"]


def _insert_runtime_order_intent(db):
    db.conn.execute(
        """
        INSERT INTO runtime_order_intents(
            intent_id, trade_date, source, mode, dry_run, status, code, side,
            created_at, updated_at
        ) VALUES ('intent-1', ?, 'test', 'OBSERVE', 1, 'CREATED', '000001', 'BUY', ?, ?)
        """,
        (TRADE_DATE, NOW.isoformat(), NOW.isoformat()),
    )
    db.conn.commit()


def _save_health_sample(db, index, *, source="PIPELINE_VIEW", available=True, generation=1, checksum="ok", namespace="reboot_v2.main"):
    minute = 1 + index
    db.save_ops_runtime_health_sample(
        {
            "trade_date": TRADE_DATE,
            "bucket_at": f"2026-06-22T09:{minute:02d}:00",
            "sampled_at": f"2026-06-22T09:{minute:02d}:00",
            "runtime_cycle_count": index + 1,
            "runtime_status": "OK",
            "runtime_profile": "THEME_CORE_V3",
            "market_context_source": source,
            "market_context_available": available,
            "dashboard_generation": generation,
            "dashboard_checksum": checksum,
            "dashboard_namespace": namespace,
        }
    )
